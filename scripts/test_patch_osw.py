#!/usr/bin/env python3
"""Fresh pinned-source and idempotent patch regression. No app is launched."""
import hashlib
import importlib.util
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("patch_osw", ROOT / "scripts/patch_osw.py")
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)
checkout = ROOT / "repos/OpenSuperWhisper"
if not (checkout / ".git").exists():
    raise SystemExit("Run python3 scripts/patch_osw.py first to generate the pinned checkout.")
archive = subprocess.check_output(["git", "-C", str(checkout), "archive", patcher.UPSTREAM_REF, "OpenSuperWhisper"])

with tempfile.TemporaryDirectory(prefix="osw-patch-tests-") as directory:
    root = Path(directory)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        for member in source.getmembers():
            if not member.isfile():
                continue
            path = root / member.name
            if not path.resolve().is_relative_to(root.resolve()):
                raise AssertionError("unexpected archive path")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.extractfile(member).read())
    patcher.APP = root / "OpenSuperWhisper"
    steps = [
        patcher.add_engine_file, patcher.patch_preferences, patcher.patch_service,
        patcher.patch_settings, patcher.patch_combined_engine_selection,
        patcher.patch_local_credentials, patcher.patch_shortcut_behavior,
        patcher.patch_cloudflare_setup, patcher.patch_menu_bar,
        patcher.patch_onboarding, patcher.patch_language_util,
        patcher.patch_fluidaudio_engine, patcher.patch_transcription_settings,
        patcher.patch_failure_paths, patcher.patch_content_view,
    ]
    def snapshot():
        return {str(p.relative_to(patcher.APP)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in patcher.APP.rglob("*.swift")}
    for step in steps:
        step()
    first = snapshot()
    for step in steps:
        step()
    assert snapshot() == first, "patch rerun changed generated source"
    settings = (patcher.APP / "Settings.swift").read_text()
    assert settings.count('Picker("Engine",') == 1
    assert 'Picker("Provider",' not in settings
    start = settings.index('Picker("Engine",')
    end = settings.index('.padding(.bottom, 8)', start)
    picker = settings[start:end]
    for required in ['SpeechRecognitionChoice.allCases', '.labelsHidden()',
                     '.accessibilityLabel("Engine")', '.frame(maxWidth: .infinity)']:
        assert required in picker, f"missing picker layout contract: {required}"
    assert 'credentialStorageMessage' in settings
    assert 'isRefreshingCloudCredentials' in settings
    assert 'it stays in this Mac\'s Keychain' not in settings
    assert (patcher.APP / "Utils/LocalCredentialStore.swift").exists()
    print("PASS fresh full patch and idempotent rerun, one accessible label-hidden full-width engine bar and local credential UI")
