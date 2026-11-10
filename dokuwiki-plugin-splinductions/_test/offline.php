<?php
/**
 * Offline check of the parser and builder, without DokuWiki.
 * usage: php _test/offline.php <pages dir> [<pages dir> ...]
 * Each dir holds <namespace path>/<page>.txt or flat files named ns__ns__page.txt.
 */
spl_autoload_register(function ($c) {
    $p = 'dokuwiki\\plugin\\splinductions\\';
    if (str_starts_with($c, $p)) require __DIR__ . '/../' . substr($c, strlen($p)) . '.php';
});

use dokuwiki\plugin\splinductions\Builder;

$pages = [];
foreach (array_slice($argv, 1) as $arg) {
    [$dir, $ns] = array_pad(explode('=', $arg, 2), 2, '');
    foreach (glob("$dir/*.txt") as $f) {
        $id = ($ns !== '' ? "$ns:" : '') . basename($f, '.txt');
        $pages[$id] = file_get_contents($f);
    }
}
$read = fn($id) => isset($pages[$id]) ? ['text' => $pages[$id], 'rev' => 1700000000, 'version' => '1.0'] : null;
$resolve = function ($ctx, $t) {
    $t = trim($t);
    if (str_starts_with($t, ':')) return strtolower(ltrim($t, ':'));
    if (str_contains($t, ':')) return strtolower($t);
    $ns = substr($ctx, 0, strrpos($ctx, ':'));
    return strtolower("$ns:$t");
};
$b = new Builder($read, $resolve);
$cands = array_values(array_filter(array_keys($pages), fn($id) => $b->isInduction($pages[$id])));
$bundle = $b->build($cands);
fwrite(STDERR, "errors:\n  " . implode("\n  ", $b->errors) . "\nwarnings:\n  " . implode("\n  ", $b->warnings) . "\nskipped: " . json_encode($b->skipped) . "\n");
if ($bundle) echo json_encode($bundle, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES), "\n";
