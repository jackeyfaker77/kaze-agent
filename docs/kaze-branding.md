# Kaze / かぜ

Selected identity: the sitting wind-ferret, with cream fur, a sage neckerchief,
and an upright blue tail. Used for both the desktop app icon and chat avatar.
The existing animated desktop pet is unchanged.

## Assets

- `assets/branding/kaze-master.png`: transparent production master.
- `assets/kaze-app-icon.png`: 512 px PNG.
- `assets/kaze-app-icon.ico`: 16, 20, 24, 32, 40, 48, 64, 128, 256 px frames.
- `apps/desktop/renderer/public/assets/branding/kaze-avatar.png`: UI avatar.
- `apps/desktop/renderer/public/assets/branding/kaze-favicon.ico`: browser icon.

Regenerate the packaged sizes on Windows with `./scripts/build-kaze-icons.ps1`.
The script preserves alpha and resamples for small-size legibility; it does not
redraw the character or claim to convert the illustration to a native 64 px sprite.

## Integration and compatibility

The product name, main window title, tray labels, app/package icon, page title,
assistant message headers, and workbench branding use Kaze and the wind-ferret.
At the user's request, the top navigation alone uses a blue wind-scroll emblem
(`assets/branding/kaze-wind-master.png`, UI copy at
`apps/desktop/renderer/public/assets/branding/kaze-wind.png`). The app icon,
favicon, chat avatar and workbench mascot continue to use the sitting ferret.
The internal package names are `kaze-agent` and `kaze-desktop`; root pnpm scripts
target `kaze-desktop`. Start from the existing repository with `pnpm start`.
The repository directory has not been renamed. External scripts that explicitly
filter the old desktop package must now use `pnpm --filter kaze-desktop`.
The existing application ID (`com.hasaki.agent`), environment
variables, internal bridge protocol, theme storage key, workspace location and
Chromium profile location remain stable so existing data and launch scripts work.
The generated installer product name is Kaze; an installer was not produced as
part of this branding change. Existing installed shortcuts require a new build.

## Artwork provenance

Created using the built-in `image_gen` tool. The user selected the sitting-pose
candidate (source generation `exec-a19ced02-0432-49bf-9aef-9f3b34c6b614.png`), then
the tool produced the transparent master (`exec-085a424c-b394-4d2f-824e-09d4cd3070cb.png`).
The source candidate used a nostalgic GBA-era pixel-art direction. The original
character identity and selected pose are preserved in the production master.

Final image-edit prompt:

> Use case: background-extraction. This is the SELECTED final Kaze wind-ferret sitting avatar. Edit this exact artwork, do not redesign it. Remove only the pale cream background and produce true transparent alpha outside the mascot, including enclosed gaps between the tail and head. Preserve the entire original sitting pose, face, tiny peach nose, round ears, cream opaque fur, sage triangular neckerchief, upright denim-blue S-shaped tail on the right, crisp pixel-art navy outlines and all original colors. Keep the cream-colored mascot fully opaque, do not remove its white/cream fur. Preserve square composition and full uncropped character. Trim only excessive empty margins so the full character occupies about 88 percent of square canvas with even transparent safety margin. No new text or badge or elements, no floor shadow, no glow, no added texture, no white matte, no checkerboard baked into image. This transparent PNG is the production master for the app icon and avatar.

## Validation

- Desktop typecheck and full main/preload/renderer build passed.
- Application identity, workspace paths, window lifecycle and package-layout
  checks: 11 tests passed.
- Browser preview using the actual React components and synthetic session data:
  light/dark chat avatars and dark workbench mark checked visually.
- No real chat request, account authorization or workspace content was changed.

## Navigation emblem follow-up

Generated with built-in `image_gen`, using the user's blue wind-scroll image as
a style reference. Production generation: `exec-d91b0a96-c4cc-4a6b-88e8-9980dd3dfee3.png`.
Transparent master copied into the workspace; the UI copy is resampled to 128 px
and displayed at 28 CSS px. Both dark and light navigation are visually checked.

Generation prompt:

> Use case: logo-brand. The supplied image is a STYLE REFERENCE ONLY, not an edit target. Create an ORIGINAL small desktop brand emblem for Kaze inspired by its flowing blue wind curls. Single isolated mark on TRUE TRANSPARENT alpha background, square canvas. Mark consists of two or three broad interlocking tapered ribbons of wind sweeping diagonally upwards, with one prominent inward spiral and a smaller secondary curl; airy asymmetric motion, compact cohesive near-circular silhouette. Rich cobalt outer edge, clear azure body, restrained icy-cyan highlights following the curves, nostalgic early-2000s Japanese fantasy RPG elemental menu icon. Simplify considerably versus reference so recognizable at 28px: thick shapes and generous open transparent negative space, no fine spirals, no flecks, no individual hair-thin lines. Fill about 85 percent of canvas with safety margin. Crisp smooth edges, illustrative flat cel-shaded bands, no fuzzy glow, no cast shadow, no photorealism, no square tile, no dark background, no cloud, no character, no text or lettering, no watermark. Design a WIND emblem rather than an ocean wave with foam. Deliver one clean transparent icon, not a comparison board or mockup.
