# FE-A handoff (branch refactor/fea, worktree /tmp/wt-fea)

The contract is /tmp/tt-refactor/PLAN.md. FE-A owns ui.css, ui.js, js/lib/*, js/vocab.js, index.html, css/index.css, js/index.js, tests/e2e/test_library.py, and the README 界面 section.
Do NOT touch the trainer files (FE-B) or the backend.

## Agreed with the coordinator
- List item `practice.last` gets `a_n` / `b_n` (1-based bar positions, null if the id is gone, a_n <= b_n). BE is adding these.
- The trainer resume link is `/trainer?id=...&resume=1`, and FE-B has confirmed it.

## Done (committed in 83a86df, wip)
1. ES modules. `app/static/ui.js` is now an ES module with named exports plus `UI` (also the default export), and it keeps the `window.UI = UI` shim.
   `$` and `esc` are exported from ui.js. index.html loads `/static/js/index.js` as `type="module"`.
   New `js/lib/theme-boot.js` is a classic script that goes in `<head>` and sets data-theme before first paint. trainer.html should include it too (FE-B).
   NOTE: trainer.html still loads ui.js as a classic script, so it breaks with a SyntaxError on `export` until FE-B switches to `type="module"`.
2. `js/lib/api.js`: get/post/put/del/upload/download, ApiError(detail, status, data), withBusy, poll. Node tests are in tests/js/lib.test.mjs and pass.
3. `js/vocab.js`: INSTRUMENT, STRINGS, MAX_FRET, STANDARD, TICKS_PER_QUARTER, GRID, TECHS, TECH_SYM, TECH_NAME, DIRECTIONS, SOURCES, WARN_CODES, parseDur.
4. ui.js overlays: layer / hasLayer / topLayer, menu (rebuilt on layer, with arrow keys, toggles when the anchor is clicked again), confirm / prompt (styled <dialog>), notice, initShell, seg (aria-pressed, role=group), tabs (tablist/tab/aria-selected, arrow keys).
5. ui.css tokens: --fs-1..4, --r-sm/md/lg (with --radius kept as an alias of r-lg), --sp-1..4, --paper-bg, --shadow-sm/md (--shadow is still used for popovers), --backdrop, --knob, --knob-shadow, --red-fg, --placeholder in both themes, and fretboard vars --fb-wood/fret/nut/string/dot/on/on-fg/next/label.
   There is a single dark selector `:root[data-theme="dark"]`. The unused --card-edge, --tab-*, --blue-soft and .tile were removed.
   Generic components now in ui.css: .grow, .card-h, .chips/.chip, .legend, .btn[aria-pressed=true], .dlg (dialogs), .sr-only.
6. js/index.js is fully rewritten to use api.js, poll, UI.confirm/prompt, the new fields (busy, recognizing, needs_review_bars, notice, practice), practice line, 接着练, sort (default 最近练习), source icon, keyboard drop zone, labels, diff-updated rows that keep focus, and `window.tt = {reload, refresh, openPages, songs}` for e2e.
   index.html now has labels/aria, tablist nav, 原谱 wording, and `.dlg pages-dlg`.

## Half-done / next steps
- css/index.css still needs updating for the new markup: `.src-ic` tile, `.song-row .title` link, `.song-row .practice` line, `.tag` badge buttons, `.notice-badge` (ellipsis), `.err-text` / `.ok-text`, `.spinner.center`, `.foot-hint`, `.lib-stack` (margin-top 12px), `.w-model` (170px), `.w-url` (260px), and drop-zone focus-visible.
  Also replace `.tag-filter` with .chips, `.page-card .thumb` background with var(--paper-bg), and `.pages-dlg::backdrop` with the .dlg backdrop. Delete the `.select-bar .grow` / `.lib-tools .grow` / `.pd-h .grow` duplicates.
- tests/e2e/test_library.py needs updating: `loadSongs()` becomes `tt.reload()`, `openPages(` becomes `tt.openPages(`, `$('search')` becomes `document.getElementById('search')`, and the confirm becomes a click on `.dlg [data-ok]` instead of `window.confirm`. The menu label 管理页面 is now 管理原谱页面.
- Not yet run in a browser at all. Smoke-test on port 8881 with TAB_TRAINER_DATA=/tmp/fea-data, then screenshot light and dark at 1440 and 1100.
- After that, message main with "FE-A foundation ready at <commit>" and an API summary.
- Merge refactor/be when the coordinator says it's ready, then verify the practice / needs_review fields.
- README 界面 section: mention ES modules, ui.js/api.js/vocab.js, tokens, and the overlay stack.
- Run scripts/check.sh and `uv run pytest -q -m e2e`. Trainer e2e is expected to fail until FE-B's module migration lands.
