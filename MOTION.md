---
schema: design-pipeline.motion-foundation.v0.1
name: Multica 本地版一键部署包 motion language
posture: minimal
primitiveRegistry: design-pipeline.motion-primitives.v1
---

## Motion Thesis

Motion confirms a stable private connection; it must never compete with command output or imply work is still progressing.

## Motion Principles

Use one slow loop, low travel, and an unhurried pause at rest. Preserve a stable silhouette and never animate critical instructions or status copy.

## Motion Vocabulary

- primitive: transform.orbit — a 3 s clockwise micro-orbit around the resting position, radius 2 px horizontally and 2 px vertically.

## Procedural Motion

No procedural runtime is used. The shipped GIF is a finite raster sequence with deterministic frame order and no random variation.

## Runtime Policy

The asset uses GIF playback only; there is no JavaScript, CSS, Canvas, or runtime dependency. GIF is supported for README and setup surfaces; PNG is the documented static fallback.

## Reduced Motion

Substitute/fallback: use the resting PNG instead of the GIF when motion reduction is requested, animation is disabled, or the asset is displayed inside a dense operational screen.

## Source Decisions

- Adopted source property: `transform.orbit` from the bundled primitive registry supplies the semantic contract for a calm, bounded idle loop.
- Rejected source property: no external animation implementation, character animation, or code was copied; kinetic loading/spinner behavior is deliberately excluded.
