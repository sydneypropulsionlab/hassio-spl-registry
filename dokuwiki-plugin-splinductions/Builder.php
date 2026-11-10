<?php

namespace dokuwiki\plugin\splinductions;

/**
 * Builds the induction bundle (format 1, the same JSON the SPL Inductions app imports) from
 * induction checklist pages.
 *
 * Page text comes from a reader callback so the builder can be pointed at approved revisions
 * (publishing) or at an unsaved draft (checking a page on save):
 *     $read(string $pageId): ?array{text:string, rev:int, version:string, approved?:string, approved_by?:string}
 * and wiki links are resolved relative to the page they appear on with
 *     $resolve(string $contextPageId, string $target): string
 */
class Builder
{
    public const ITEM_RE = '/^\[(\s?|x|demo|ack)\]\s*(.*)$/i';
    public const KINDS = ['' => 'check', ' ' => 'check', 'x' => 'check', 'demo' => 'demo', 'ack' => 'ack'];
    public const REF_RE = '/^\s*(?:(?:swp|ra)\s*[:\-]\s*)?(hazard|step|section|intro|introduction|risk|emergency)\b\s*[:\-]?\s*(.*)$/i';

    /** @var callable */
    protected $read;
    /** @var callable */
    protected $resolve;
    /** @var callable|null reads SWP/RA pages; defaults to $read */
    protected $readSource;
    protected string $tag;
    protected array $swpNs;
    protected array $raNs;

    public array $errors = [];
    public array $warnings = [];
    /** @var array<string,string> pages left out of the bundle => reason */
    public array $skipped = [];
    /** @var array<string,array> page id => induction built from it */
    public array $byPage = [];
    /** @var array<string,string> page => induction id already published (used for held pages) */
    public array $knownIds = [];
    /** @var string[] pages whose approved revision could not be built (they keep their last published version) */
    public array $failed = [];

    /** @var array<string,Source|false> */
    protected array $sources = [];
    /** @var array<string,array|null> */
    protected array $texts = [];

    public function __construct(callable $read, callable $resolve, string $tag = 'induction',
                                array $swpNs = ['whs:swp'], array $raNs = ['whs:ra'], ?callable $readSource = null)
    {
        $this->read = $read;
        $this->readSource = $readSource;
        $this->resolve = $resolve;
        $this->tag = strtolower($tag);
        $this->swpNs = $swpNs;
        $this->raNs = $raNs;
    }

    protected function readPage(string $id): ?array
    {
        if (!array_key_exists($id, $this->texts)) {
            $r = ($this->read)($id);
            $this->texts[$id] = ($r && trim((string)$r['text']) !== '') ? $r : null;
        }
        return $this->texts[$id];
    }

    /** True when the text carries the induction tag. */
    public function isInduction(string $text): bool
    {
        return in_array($this->tag, WikiText::tags($text), true);
    }

    protected function inNs(string $id, array $namespaces): bool
    {
        foreach ($namespaces as $ns) {
            $ns = trim($ns, ': ');
            if ($ns !== '' && str_starts_with($id, $ns . ':')) return true;
        }
        return false;
    }

    /**
     * An SWP or RA page. These only supply the text attached to checklist items, so a missing
     * page is a warning, never a reason to hold an induction back.
     */
    protected function source(string $id, string $kind, string $where): ?Source
    {
        if (!array_key_exists($id, $this->sources)) {
            $r = $this->readSource ? ($this->readSource)($id) : $this->readPage($id);
            if ($r && trim((string)$r['text']) === '') $r = null;
            $this->sources[$id] = $r ? new Source($kind, $id, $r) : false;
        }
        if (!$this->sources[$id]) {
            $this->warnings[] = "$where: [[$id]] does not exist, so its items have no SWP/RA text attached";
        }
        return $this->sources[$id] ?: null;
    }

    /** Page ids linked in a header cell, plus any plain text left over. */
    protected function cellLinks(string $page, ?string $raw): array
    {
        $ids = [];
        if ($raw === null) return [[], ''];
        $rest = preg_replace_callback(WikiText::LINK_RE, function ($m) use ($page, &$ids) {
            $l = WikiText::link($m[1]);
            if (!$l['external'] && $l['target'] !== '') $ids[] = ($this->resolve)($page, $l['target']);
            return ' ';
        }, $raw);
        return [$ids, WikiText::plain($rest)];
    }

    protected static function listCell(string $plain): array
    {
        return array_values(array_filter(array_map('trim', preg_split('/\s*[,;]\s*|\s{2,}/', $plain)), 'strlen'));
    }

    /** The induction id written in a page's header table (read without building it). */
    protected function inductionIdOf(string $page): ?string
    {
        $id = $this->byPage[$page]['id'] ?? '';
        if ($id === '' && ($r = $this->readPage($page))) {
            $id = (new WikiDoc($r['text']))->metaPlain(['induction id', 'id']);
        }
        if ($id === '') $id = $this->knownIds[$page] ?? '';
        return $id !== '' ? $id : null;
    }

    /**
     * Build one induction from a page.
     * @return array|null null when the page is not an induction (no tag) or cannot be read
     */
    public function buildInduction(string $page): ?array
    {
        $r = $this->readPage($page);
        if (!$r) {
            $this->skipped[$page] = 'no approved revision';
            return null;
        }
        if (!$this->isInduction($r['text'])) {
            $this->skipped[$page] = "the approved revision is not tagged {$this->tag}";
            return null;
        }
        $where = $page;
        $doc = new WikiDoc($r['text']);
        $errorsBefore = count($this->errors);

        $indId = $doc->metaPlain(['induction id']);
        if ($indId === '') $this->errors[] = "$where: the header table needs an \"Induction ID\" row";
        elseif (!preg_match('/^[A-Za-z0-9_.-]+$/', $indId)) {
            $this->errors[] = "$where: Induction ID \"$indId\" may only contain letters, digits, _ . and -";
        }
        $title = $doc->title;
        if ($title === '') $this->errors[] = "$where: missing the ====== title ======";
        $type = strtolower($doc->metaPlain(['type']));
        if (!in_array($type, ['equipment', 'space'], true)) {
            $this->errors[] = "$where: Type must be \"equipment\" or \"space\"" . ($type !== '' ? ", not \"$type\"" : '');
        }
        $location = $doc->metaPlain(['location']);
        if ($location === '') $this->errors[] = "$where: the header table needs a \"Location\" row";

        $refreshRaw = $doc->metaPlain(['refresh']);
        $refresh = 12;
        if ($refreshRaw !== '') {
            if (preg_match('/^(\d+)/', $refreshRaw, $m)) $refresh = (int)$m[1];
            else $this->errors[] = "$where: Refresh must be a whole number of months (0 for no expiry)";
        }
        $self = in_array(strtolower($doc->metaPlain(['self service', 'selfservice'])), ['yes', 'y', 'true', '1'], true);

        // sources named in the header
        $headSources = [];
        foreach (['swp' => ['swp'], 'ra' => ['ra', 'risk assessment']] as $kind => $keys) {
            [$ids, $rest] = $this->cellLinks($page, $doc->metaRaw($keys));
            if (!$ids && $rest !== '' && !in_array(strtolower($rest), ['-', 'none', 'n/a'], true)) {
                $ids[] = ($this->resolve)($page, $rest);
            }
            foreach ($ids as $id) $headSources[$id] = $kind;
        }
        $sources = [];
        foreach ($headSources as $id => $kind) {
            $src = $this->source($id, $kind, $where);
            if ($src) $sources[$id] = $src;
        }

        // grants access: "Name" or "id: Name", separated by commas
        $resources = [];
        foreach (self::listCell($doc->metaPlain(['grants access', 'access'])) as $entry) {
            if (in_array(strtolower($entry), ['-', 'none'], true)) continue;
            if (preg_match('/^([^:]+):\s*(.+)$/', $entry, $m)) {
                $rid = WikiText::slug($m[1]);
                $name = trim($m[2]);
            } else {
                $rid = WikiText::slug($entry);
                $name = $entry;
            }
            if ($rid === '' || $name === '') {
                $this->errors[] = "$where: Grants access entry \"$entry\" should be a name or \"id: Name\"";
                continue;
            }
            $resources[] = ['id' => $rid, 'name' => $name];
        }

        // requires: links to other induction pages, or induction ids
        $requires = [];
        [$reqPages, $reqRest] = $this->cellLinks($page, $doc->metaRaw(['requires']));
        foreach ($reqPages as $rp) {
            $rid = $this->inductionIdOf($rp);
            if ($rid === null) $this->warnings[] = "$where: requires [[$rp]], which has no approved revision with an Induction ID yet";
            else $requires[] = $rid;
        }
        foreach (self::listCell($reqRest) as $tok) {
            if (!in_array(strtolower($tok), ['-', 'none'], true)) $requires[] = $tok;
        }

        // checklist sections
        $sections = [];
        foreach ($doc->sections as $si => $s) {
            $items = [];
            foreach ($s['lines'] as $line) {
                $t = trim($line);
                if ($t === '' || $t[0] === '^' || $t[0] === '|' || WikiDoc::isMarkupOnly($t)) continue;
                if (preg_match('/^\s*={2,6}.*={2,6}\s*$/', $line)) continue;
                if (preg_match('/^( {2,}|\t+)[*-]\s+(.*)$/', $line, $m)) {
                    $content = $m[2];
                    if (preg_match(self::ITEM_RE, $content, $im)) {
                        $kind = self::KINDS[strtolower($im[1])];
                        $content = $im[2];
                    } else {
                        $kind = 'info';
                    }
                } else {
                    $kind = 'info';
                    $content = $t;
                }
                [$text, $refs] = $this->itemRefs($page, $content, $headSources, $sources);
                if ($text === '' && !$refs) continue;
                $items[] = ['id' => ($si + 1) . '.' . (count($items) + 1), 'kind' => $kind, 'text' => $text,
                    'refs' => $refs];
            }
            if ($items) $sections[] = ['title' => $s['title'], 'items' => $items];
        }
        $hasTicks = false;
        foreach ($sections as $s) foreach ($s['items'] as $it) if ($it['kind'] !== 'info') $hasTicks = true;
        if (!$hasTicks) $this->errors[] = "$where: no checklist items (\"  * [ ] ...\", \"  * [demo] ...\" or \"  * [ack] ...\")";

        $ind = [
            'id' => $indId,
            'title' => $title,
            'type' => $type,
            'location' => $location,
            'description' => $doc->intro,
            'refresh_months' => $refresh,
            'self_service' => $self,
            'grants_access' => array_column($resources, 'name'),
            'resources' => $resources,
            'requires' => array_values(array_unique($requires)),
            'sources' => array_values(array_map(fn(Source $s) => $s->summary(), $sources)),
            'sections' => $sections,
            'page' => $page,
        ];
        $ind['version'] = substr(hash('sha256', self::canonical($ind)), 0, 12);
        $ind['wiki'] = ['page' => $page, 'rev' => (int)$r['rev'], 'version' => (string)($r['version'] ?? ''),
            'approved' => $r['approved'] ?? null, 'approved_by' => $r['approved_by'] ?? null];
        $ind['_ok'] = count($this->errors) === $errorsBefore;
        $this->byPage[$page] = $ind;
        return $ind;
    }

    /** Pull SWP/RA references out of an item's text. Returns [plain text, refs]. */
    protected function itemRefs(string $page, string $content, array $headSources, array &$sources): array
    {
        $refs = [];
        $where = $page;
        $text = preg_replace_callback(WikiText::LINK_RE, function ($m) use ($page, $where, $headSources, &$sources, &$refs) {
            $l = WikiText::link($m[1]);
            if ($l['external']) return $m[0];
            $id = $l['target'] === '' ? $page : ($this->resolve)($page, $l['target']);
            $isSwp = $this->inNs($id, $this->swpNs) || ($headSources[$id] ?? '') === 'swp';
            $isRa = $this->inNs($id, $this->raNs) || ($headSources[$id] ?? '') === 'ra';
            if (!$isSwp && !$isRa) return $m[0];
            $label = $l['label'] ?? '';
            if (preg_match(self::REF_RE, $label, $rm)) {
                $what = strtolower($rm[1]);
                $arg = trim($rm[2]);
            } elseif ($label === '' && $l['anchor'] !== '') {
                $what = 'section';
                $arg = str_replace('_', ' ', $l['anchor']);
            } else {
                if (str_contains($label, ':')) {
                    $this->warnings[] = "$where: link \"$label\" to [[$id]] is not a reference (start the label with "
                        . "hazard:, step, section:, intro, risk: or emergency:)";
                }
                return $m[0];
            }
            $src = $sources[$id] ?? $this->source($id, $isRa ? 'ra' : 'swp', $where);
            if (!$src) return '';
            $sources[$id] = $src;
            $res = $src->resolve($what, $arg);
            if ($res === null) {
                $this->warnings[] = "$where: reference \"" . ($label !== '' ? $label : "#{$l['anchor']}")
                    . "\" does not match anything in [[$id]]" . ($src->version !== '' ? " (v{$src->version})" : '')
                    . "; the item is published without that text";
            } else {
                $refs[] = $res;
            }
            return "\x03";
        }, $content);
        // drop the reference links and any brackets left around them
        $text = preg_replace('/\(\s*(?:\x03[\s,;]*)+\)/', '', $text);
        $text = str_replace("\x03", '', $text);
        $text = WikiText::plain($text);
        $text = preg_replace('/\s+([,.;:)])/', '$1', $text);
        return [trim($text), $refs];
    }

    /**
     * Build the bundle from candidate pages (pages tagged with the induction tag). Pages with
     * errors are left out and listed in $failed; the caller decides what to do with them.
     */
    public function build(array $pages): array
    {
        sort($pages);
        $inductions = [];
        foreach ($pages as $page) {
            $ind = $this->buildInduction($page);
            if ($ind) $inductions[] = $ind;
        }
        $this->crossCheck($inductions);
        $bad = [];
        foreach ($this->errors as $e) {
            if (preg_match('/^([^ ]+): /', $e, $m)) $bad[$m[1]] = true;
        }
        $ok = [];
        foreach ($inductions as $i) {
            if (isset($bad[$i['page']]) || !$i['_ok']) {
                $this->failed[] = $i['page'];
                $this->byPage[$i['page']]['_ok'] = false;
                continue;
            }
            unset($i['_ok']);
            $ok[] = $i;
        }
        return self::bundle($ok);
    }

    /** Wrap inductions in a bundle, sorted, with a hash of the checklist content. */
    public static function bundle(array $inductions): array
    {
        usort($inductions, fn($a, $b) => strcmp($a['id'], $b['id']));
        $bundle = [
            'format' => 1,
            'generated_at' => gmdate('Y-m-d\\TH:i:s\\Z'),
            'generator' => 'splinductions DokuWiki plugin',
            'inductions' => $inductions,
        ];
        $bundle['hash'] = substr(hash('sha256', self::canonical(array_map(function ($i) {
            unset($i['wiki']);
            return $i;
        }, $inductions))), 0, 12);
        return $bundle;
    }

    public function crossCheck(array $inductions): void
    {
        $ids = [];
        $known = array_flip($this->knownIds);
        foreach ($inductions as $ind) {
            if ($ind['id'] === '') continue;
            if (isset($ids[$ind['id']])) {
                $this->errors[] = "{$ind['page']}: Induction ID {$ind['id']} is also used by {$ids[$ind['id']]}";
            } else {
                $ids[$ind['id']] = $ind['page'];
            }
        }
        $names = [];
        foreach ($inductions as $ind) {
            foreach ($ind['requires'] as $req) {
                if ($req === $ind['id']) $this->errors[] = "{$ind['page']}: an induction cannot require itself";
                elseif (!isset($ids[$req]) && !isset($known[$req])) {
                    $this->warnings[] = "{$ind['page']}: requires $req, which is not a published induction yet";
                }
            }
            foreach ($ind['resources'] as $res) {
                if (!isset($names[$res['id']])) {
                    $names[$res['id']] = [$res['name'], $ind['id']];
                } elseif ($names[$res['id']][0] !== $res['name']) {
                    $this->errors[] = "{$ind['page']}: access id \"{$res['id']}\" is \"{$res['name']}\" here but "
                        . "\"{$names[$res['id']][0]}\" in {$names[$res['id']][1]}; give them different ids or the same name";
                }
            }
        }
        foreach ($inductions as $ind) {
            if ($ind['type'] !== 'equipment' || $ind['requires']) continue;
            $spaces = [];
            foreach ($inductions as $o) {
                if ($o['type'] === 'space' && $o['location'] === $ind['location']) $spaces[] = $o['id'];
            }
            if ($spaces) {
                $this->warnings[] = "{$ind['page']}: {$ind['id']} is in {$ind['location']} but does not require "
                    . implode(', ', $spaces);
            }
        }
    }

    /** JSON with object keys sorted, for hashing (matches Python's json.dumps(sort_keys=True) closely enough). */
    public static function canonical($v): string
    {
        $sort = function ($x) use (&$sort) {
            if (!is_array($x)) return $x;
            if (array_is_list($x)) return array_map($sort, $x);
            ksort($x);
            return array_map($sort, $x);
        };
        return json_encode($sort($v), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    }
}
