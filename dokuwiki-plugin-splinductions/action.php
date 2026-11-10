<?php

use dokuwiki\Extension\ActionPlugin;
use dokuwiki\Extension\Event;
use dokuwiki\Extension\EventHandler;

/**
 * When the bundle is rebuilt:
 *  - right after a page is approved (the approve plugin redirects, so this runs at shutdown);
 *  - in the background task runner whenever approvals or tagged pages have changed (a safety net
 *    that also catches deletions, moves and approvals made any other way).
 * On save, an induction page is checked as a draft and the editor sees any problems at once.
 */
class action_plugin_splinductions extends ActionPlugin
{
    public function register(EventHandler $controller)
    {
        $controller->register_hook('COMMON_WIKIPAGE_SAVE', 'AFTER', $this, 'handleSave');
        // before the approve plugin, which redirects and exits once it has stored the approval
        $controller->register_hook('ACTION_ACT_PREPROCESS', 'BEFORE', $this, 'handleApprove', null, -100);
        $controller->register_hook('INDEXER_TASKS_RUN', 'BEFORE', $this, 'handleTaskRunner');
        $controller->register_hook('TPL_ACT_RENDER', 'BEFORE', $this, 'handlePageStatus');
    }

    protected function helper(): helper_plugin_splinductions
    {
        return plugin_load('helper', 'splinductions');
    }

    protected function isWatched(string $id, string $text): bool
    {
        $h = $this->helper();
        if ($h->isTemplate($id) || !$h->inNamespace($id)) return false;
        return $h->isInductionText($text) || isset($h->status()['pages'][$id]);
    }

    public function handleSave(Event $event)
    {
        $id = $event->data['id'];
        $text = (string)$event->data['newContent'];
        if (!$this->isWatched($id, $text)) return;
        $h = $this->helper();
        if (!$h->isInductionText($text)) {
            msg(sprintf($this->getLang('untagged'), $h->tag()), 2);
            return;
        }
        [$errors, $warnings] = $h->checkDraft($id, $text);
        foreach (array_slice($errors, 0, 15) as $e) msg(hsc($this->getLang('check_error') . ' ' . $e), -1);
        if (count($errors) > 15) msg(sprintf($this->getLang('more_errors'), count($errors) - 15), -1);
        foreach (array_slice($warnings, 0, 5) as $w) msg(hsc($this->getLang('check_warning') . ' ' . $w), 2);
        if (!$errors) {
            msg($h->requireApproval() ? $this->getLang('check_ok_approve') : $this->getLang('check_ok'), 1);
            if (!$h->requireApproval()) register_shutdown_function([$h, 'rebuild'], "saved $id");
        }
    }

    public function handleApprove(Event $event)
    {
        global $INPUT, $ID;
        if ($event->data !== 'show' || !$INPUT->has('approve')) return;
        $h = $this->helper();
        register_shutdown_function(function () use ($h, $ID) {
            try {
                $h->rebuild("approved $ID");
            } catch (\Throwable $e) {
                \dokuwiki\Logger::error('splinductions: rebuild after approval failed', $e->getMessage());
            }
        });
    }

    public function handleTaskRunner(Event $event)
    {
        $h = $this->helper();
        $status = $h->status();
        if (($status['fingerprint'] ?? '') === $h->fingerprint()) return;
        $event->preventDefault();
        $event->stopPropagation();
        $h->rebuild('background check');
        echo "splinductions: induction bundle rebuilt\n";
    }

    /** A short note above an induction page: is this revision what the app is using? */
    public function handlePageStatus(Event $event)
    {
        global $ID, $INFO;
        if ($event->data !== 'show' || !page_exists($ID)) return;
        $h = $this->helper();
        if ($h->isTemplate($ID) || !$h->inNamespace($ID)) return;
        $status = $h->status();
        $pub = $status['pages'][$ID] ?? null;
        $failed = in_array($ID, $status['failed_pages'] ?? [], true);
        if (!$pub && !$failed && !$h->isInductionText(rawWiki($ID))) return;

        $mine = fn($m) => str_starts_with($m, "$ID: ");
        $errs = array_values(array_filter($status['errors'] ?? [], $mine));
        $warns = array_values(array_filter($status['warnings'] ?? [], $mine));
        if ($errs) {
            $cls = 'error';
            $msg = $pub ? sprintf($this->getLang('page_held'), hsc($pub['id']), hsc($pub['wiki_version']))
                : $this->getLang('page_errors');
        } elseif ($pub) {
            $current = (int)($INFO['lastmod'] ?? 0) === (int)$pub['rev'];
            $msg = sprintf($this->getLang($current ? 'page_published' : 'page_published_older'),
                hsc($pub['id']), hsc($pub['wiki_version']), dformat($pub['rev']));
            $cls = $current ? ($warns ? 'notify' : 'info') : 'notify';
        } else {
            $cls = 'notify';
            $msg = $h->requireApproval() ? $this->getLang('page_not_approved') : $this->getLang('page_not_published');
        }
        echo '<div class="plugin_splinductions_note plugin_splinductions_' . $cls . '">';
        echo '<strong>' . $this->getLang('page_heading') . '</strong> ' . $msg;
        $list = function ($items, $heading) use ($ID) {
            if (!$items) return;
            if ($heading !== '') echo '<div class="plugin_splinductions_small">' . $heading . '</div>';
            echo '<ul>';
            foreach ($items as $e) echo '<li>' . hsc(substr($e, strlen($ID) + 2)) . '</li>';
            echo '</ul>';
        };
        $list($errs, '');
        $list($warns, $this->getLang('page_warnings'));
        echo '</div>';
    }
}
