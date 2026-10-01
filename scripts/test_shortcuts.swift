// The runner appends this to the real ShortcutManager implementation, replacing
// only its external dependencies. No global input, microphone, or preferences
// are touched. Callback dispatch, hold timing and cancellation are production code.
import AppKit

struct KeyboardShortcuts {
    struct Shortcut { enum Key { case backtick, escape }; enum Modifier { case option }; init(_ key: Key, modifiers: Modifier? = nil) {} }
    struct Name: Hashable { let raw: String; init(_ raw: String, default shortcut: Shortcut? = nil) { self.raw = raw } }
    static var down: [Name: () -> Void] = [:]
    static var up: [Name: () -> Void] = [:]
    static var enabled = Set<Name>()
    static func onKeyDown(for name: Name, action: @escaping () -> Void) { down[name] = action }
    static func onKeyUp(for name: Name, action: @escaping () -> Void) { up[name] = action }
    static func enable(_ name: Name) { enabled.insert(name) }
    static func disable(_ name: Name) { enabled.remove(name) }
}
extension Notification.Name {
    static let hotkeySettingsChanged = Self("testHotkeySettingsChanged")
    static let indicatorWindowDidHide = Self("testIndicatorDidHide")
}
final class AppPreferences {
    static let shared = AppPreferences()
    var modifierOnlyHotkey = "none"
    var mouseButtonHotkey = "middle"
    var doublePressToTrigger = true
    var holdToRecord = false
}
enum ModifierKey: String { case none, leftCommand; var displayName: String { rawValue } }
final class ModifierKeyMonitor {
    static let shared = ModifierKeyMonitor()
    var onKeyDown: (() -> Void)?
    var onKeyUp: (() -> Void)?
    func start(modifierKey: ModifierKey) {}
    func stop() {}
}
final class MouseButtonMonitor {
    static let shared = MouseButtonMonitor()
    var onButtonDown: (() -> Void)?
    var onButtonUp: (() -> Void)?
    var selected = MouseButton.none
    func start(mouseButton: MouseButton) { selected = mouseButton }
    func stop() { selected = .none }
}
@MainActor final class IndicatorViewModel {
    func startRecording() { IndicatorWindowManager.shared.starts += 1 }
}
@MainActor final class IndicatorWindowManager {
    static let shared = IndicatorWindowManager()
    var starts = 0
    var stops = 0
    var cancels = 0
    func prepare() -> IndicatorViewModel { KeyboardShortcuts.enable(.escape); return IndicatorViewModel() }
    func presentWindow(for vm: IndicatorViewModel, nearPoint: NSPoint?) {}
    func stopRecording() { stops += 1 }
    func requestCancel() -> Bool {
        cancels += 1
        NotificationCenter.default.post(name: .indicatorWindowDidHide, object: nil)
        return true
    }
}
enum FocusUtils {
    static func getCurrentCursorPosition() -> NSPoint? { nil }
    static func getInputAnchorPoint() -> NSPoint? { nil }
}

@main enum ShortcutTests {
    static var failures = 0
    static func check(_ name: String, _ result: Bool) {
        print("  \(result ? "ok  " : "FAIL") \(name)")
        if !result { failures += 1 }
    }
    @MainActor static func tick(_ seconds: Double = 0.04) async { try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000)) }
    @MainActor static func main() async {
        _ = ShortcutManager.shared
        let window = IndicatorWindowManager.shared
        let prefs = AppPreferences.shared
        check("mouse mode disables keyboard recording shortcut", !KeyboardShortcuts.enabled.contains(.toggleRecord))
        MouseButtonMonitor.shared.onButtonDown?()
        await tick()
        check("hidden modifier double-press setting does not block mouse start", window.starts == 1)
        MouseButtonMonitor.shared.onButtonUp?()
        await tick()
        check("toggle mouse release does not submit recording", window.stops == 0)
        KeyboardShortcuts.up[.escape]?()
        await tick()
        check("Escape cancels mouse session without decoding", window.cancels == 1 && window.stops == 0)

        prefs.mouseButtonHotkey = "none"
        NotificationCenter.default.post(name: .hotkeySettingsChanged, object: nil)
        check("keyboard mode enables recording shortcut", KeyboardShortcuts.enabled.contains(.toggleRecord))
        KeyboardShortcuts.down[.toggleRecord]?()
        await tick()
        check("hidden modifier double-press setting does not block keyboard start", window.starts == 2)
        KeyboardShortcuts.up[.toggleRecord]?()
        KeyboardShortcuts.up[.escape]?()
        await tick()
        check("Escape cancels keyboard session without decoding", window.cancels == 2 && window.stops == 0)

        prefs.doublePressToTrigger = false
        prefs.holdToRecord = true
        let before = window.stops
        KeyboardShortcuts.down[.toggleRecord]?()
        await tick(0.36)
        KeyboardShortcuts.up[.toggleRecord]?()
        await tick()
        check("held keyboard release stops exactly once", window.stops == before + 1)
        NotificationCenter.default.post(name: .indicatorWindowDidHide, object: nil)
        prefs.mouseButtonHotkey = "middle"
        NotificationCenter.default.post(name: .hotkeySettingsChanged, object: nil)
        MouseButtonMonitor.shared.onButtonDown?()
        await tick(0.36)
        MouseButtonMonitor.shared.onButtonUp?()
        await tick()
        check("held mouse release stops exactly once", window.stops == before + 2)
        NotificationCenter.default.post(name: .indicatorWindowDidHide, object: nil)

        prefs.mouseButtonHotkey = "none"
        prefs.modifierOnlyHotkey = "leftCommand"
        prefs.doublePressToTrigger = true
        prefs.holdToRecord = false
        NotificationCenter.default.post(name: .hotkeySettingsChanged, object: nil)
        let starts = window.starts
        ModifierKeyMonitor.shared.onKeyDown?()
        ModifierKeyMonitor.shared.onKeyUp?()
        await tick()
        check("modifier first tap waits for configured double-tap", window.starts == starts)
        ModifierKeyMonitor.shared.onKeyDown?()
        await tick()
        check("modifier second tap starts recording", window.starts == starts + 1)
        KeyboardShortcuts.up[.escape]?()
        await tick()
        if failures != 0 { exit(1) }
        print("all shortcut dispatch checks passed")
    }
}
