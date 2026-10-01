// Appended to production MouseButtonMonitor.swift. Construct events but never
// post them, and configure a test instance without creating an event tap.
extension MouseButtonMonitor {
    static func testInstance(_ button: MouseButton) -> MouseButtonMonitor {
        let instance = MouseButtonMonitor()
        instance.selectedMouseButton = button
        return instance
    }
    func testEvent(_ type: CGEventType, button: Int64) -> Bool {
        let event = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: .zero, mouseButton: .center)!
        event.setIntegerValueField(.mouseEventButtonNumber, value: button)
        return handleMouseEvent(type: type, event: event)
    }
}
@main enum MouseButtonTests {
    static func check(_ name: String, _ result: Bool) {
        print("  \(result ? "ok  " : "FAIL") \(name)")
        if !result { exit(1) }
    }
    @MainActor static func main() async {
        let monitor = MouseButtonMonitor.testInstance(.middle)
        var downs = 0, ups = 0
        monitor.onButtonDown = { downs += 1 }
        monitor.onButtonUp = { ups += 1 }
        check("middle maps to CGEvent button2", MouseButton.middle.buttonNumber == 2)
        check("left is not consumed", !monitor.testEvent(.otherMouseDown, button: 0))
        check("unselected extra button is not consumed", !monitor.testEvent(.otherMouseDown, button: 3))
        check("selected middle down is consumed", monitor.testEvent(.otherMouseDown, button: 2))
        _ = monitor.testEvent(.otherMouseDown, button: 2)
        check("selected middle up is consumed", monitor.testEvent(.otherMouseUp, button: 2))
        _ = monitor.testEvent(.otherMouseUp, button: 2)
        try? await Task.sleep(nanoseconds: 40_000_000)
        check("duplicate down/up dispatch once", downs == 1 && ups == 1)
        monitor.stop()
        print("all mouse event checks passed")
    }
}
