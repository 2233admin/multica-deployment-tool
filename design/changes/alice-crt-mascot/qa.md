# QA

## Asset checks

- Source: user-selected Alice CRT artwork, 1448 x 1086 PNG.
- Static fallback: 480 x 360 PNG.
- Motion asset: 480 x 360 GIF, 12 frames at 250 ms each (3 seconds).
- Package validation includes both distributed mascot files.
- Test suite: `12 passed`.

## Motion review

The full CRT composition follows a bounded 2 px micro-orbit. It stays documentation-only, does not indicate a loading state, uses no runtime animation code, and has an explicit PNG alternative for reduced motion.

## Verdict

Approved for README documentation use. Reference claim: not applicable; the source artwork is used as the user-selected project asset rather than reproduced as a separate reference implementation.
