# FE-A handoff (branch refactor/fea, worktree /tmp/wt-fea)

The contract is /tmp/tt-refactor/PLAN.md. FE-A owns ui.css, ui.js, js/lib/*, js/vocab.js, index.html, css/index.css, js/index.js, tests/e2e/test_library.py, and the README 界面 section.
Do NOT touch the trainer files (FE-B) or the backend.

## Agreed with the coordinator
- List item `practice.last` gets `a_n` / `b_n` (1-based bar positions, null if the id is gone, a_n <= b_n). BE is adding these.
- The trainer resume link is `/trainer?id=...&resume=1` (FE-B confirmed).
- "FE-A foundation ready at dc19d76" was sent to main with the full API summary.

## Status: items 1–7 are done
- 1–5 shared foundation: ui.js (ES module plus the window.UI shim), js/lib/api.js, js/lib/theme-boot.js, js/vocab.js, ui.css tokens. Node tests in tests/js/lib.test.mjs.
- 6 library page: js/index.js, index.html, css/index.css.
- 7 duplicate helpers removed from FE-A files. Screenshots checked in light and dark at 1440 and 1100: list, menu, confirm dialog, page-management dialog, settings.
- README 界面 section rewritten.
- tests/e2e/test_library.py: 4 tests pass, including a new keyboard / overlay / focus test.
- scripts/check.sh passes.

## Remaining
- Wait for the coordinator's "BE API contract ready", then `git merge refactor/be`.
  Re-run the library e2e and screenshot with real data: busy / recognizing / needs_review_bars / notice / practice.{last_at,total_seconds,last.a_n,b_n}.
  Until then, the list shows no practice line, no busy spinner and no review badge with the old API. This is intentional: no fallback to the old fields.
- The garbled artist on the eval-r-mother* songs (GBK shown as Latin-1) is a BE migration fix and is already in the contract.
- Trainer e2e will fail until FE-B loads ui.js as `type="module"`, because a classic script hits a SyntaxError on `export`.
