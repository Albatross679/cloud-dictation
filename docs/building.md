# Building

## Requirements

| Need | Why | Install |
|---|---|---|
| Xcode | the app is an `.xcodeproj` | App Store, then `sudo xcode-select -s /Applications/Xcode.app/Contents/Developer && sudo xcodebuild -license accept`, then `xcodebuild -runFirstLaunch` |
| cmake | builds `libwhisper` | `brew install cmake` |
| Rust | builds the `asian-autocorrect` dylib the bridging header imports | `curl https://sh.rustup.rs -sSf \| sh` |
| libomp | linked by `libwhisper` | `brew install libomp` |

All four are required even for a cloud-only build: the bridging header pulls in whisper.cpp and asian-autocorrect, so the local engine compiles regardless. This is a cost for whoever builds the app, not for whoever installs it.

## Signing

Run this once, before the first build:

```bash
./scripts/create_signing_identity.sh
```

macOS ties permission grants to an app's designated requirement. Ad hoc signing makes that requirement a bare `cdhash`, which changes on every build, so each rebuild silently voids Accessibility, Input Monitoring, Microphone, and PostEvent while System Settings still shows them enabled.

A self-signed certificate makes the requirement name the certificate instead, so grants survive rebuilds. It is user scoped, free, and needs no Apple Developer account. Check it took:

```bash
codesign -d -r- "/Applications/OSW Cloud.app"
```

A requirement mentioning `cdhash` means the identity is missing and the build fell back to ad hoc.

The app ships as bundle id `local.clouddictation.OpenSuperWhisper`, display name `OSW Cloud`, so it coexists with a stock OpenSuperWhisper install rather than sharing its preferences. If both are installed, give them different record hotkeys; the default is Option + backtick in each.

## Packaging

```bash
./scripts/make_dmg.sh
```

Produces a drag-to-install `runs/OSW Cloud.dmg`, about 8 MB. Unsigned by Apple, so it installs on the machine that built it and Gatekeeper refuses it everywhere else.

Distribution needs an Apple Developer ID, after which the same script signs, notarizes, and staples:

```bash
./scripts/make_dmg.sh "Developer ID Application: Your Name (TEAMID)" your-notary-profile
```

The DMG format is not what makes an app distributable; notarization is. Upstream ships a notarized DMG, which is why installing OpenSuperWhisper never required Xcode.

## Reproducibility

The tracked source rebuilds both the app and the DMG. Verified by cloning the repo to a clean directory and running the pipeline: all 20 patches applied and the app built.

`repos/` and `runs/` are gitignored and regenerated, so nothing there needs backing up. Upstream is pinned to an exact commit in `scripts/patch_osw.py`, because every patch is an exact string match and a moving branch would break a rebuild on whichever anchor drifted. Move the pin deliberately with `manage.sh --sync`.

Two things do not come from the repo:

| Not reproducible | Consequence |
|---|---|
| `.auth-token.local` | A secret. Generate a new one and `wrangler secret put AUTH_TOKEN`. |
| The signing certificate | A new one is a new identity, so permissions need granting once more. |

Builds stamp their own version and provenance into `Info.plist` (`CFBundleShortVersionString`, `CDSourceRef`, `CDUpstreamRef`), so a binary traces back to the commits that produced it rather than reporting upstream's version.

## How the patching works

`scripts/patch_osw.py` clones upstream and applies every change as an exact string replacement, verified before writing. If upstream moves an anchor the script stops and names the file it could not patch, rather than producing a half-patched tree. Nothing in `repos/OpenSuperWhisper` is edited by hand.
