// Agent Office в статусной панели macOS: траты за сегодня, сессии, запуск сервера.
// Собирается scripts/build-menubar.sh в build/AgentOffice.app; без зависимостей.
// Окошко по клику — страница сервера web/menubar.html: её вид правится без пересборки.
import Cocoa
import WebKit

// ── Настройки: из Info.plist собранного приложения, иначе — по умолчанию ─────
struct Config {
    let python: String
    let server: String
    let port: Int
    let log: String

    static func load() -> Config {
        let info = Bundle.main.infoDictionary ?? [:]
        let env = ProcessInfo.processInfo.environment
        let here = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
        return Config(
            python: env["AO_PYTHON"] ?? info["AOPython"] as? String ?? "/usr/bin/python3",
            server: env["AO_SERVER"] ?? info["AOServer"] as? String
                ?? here.appendingPathComponent("../../../../server.py").standardized.path,
            port: Int(env["AO_PORT"] ?? "") ?? info["AOPort"] as? Int ?? 8767,
            log: NSHomeDirectory() + "/Library/Logs/agent-office.log")
    }

    var base: String { "http://127.0.0.1:\(port)" }
}

struct Session: Decodable {
    let id: String
    let title: String
    let project: String
    let at: Double
    let isLive: Bool
    let working: Int
    let cost: Double
    let today: Double?
}

/// Целые доллары, центы — только до доллара: в строке меню место дорого.
func money(_ usd: Double) -> String {
    if usd > 0 && usd < 0.05 { return "<$0.1" }
    return String(format: "$%.1f", usd)
}

// ── Пиксельный офис: значок в строке меню и иконка приложения из одной картинки ──
enum Pixel {
    static let office = [
        "................",
        ".....GGGGGG.....",
        ".....GGGGGG.....",
        "..RRRRRRRRRRRR..",
        "..BBBBBBBBBBBB..",
        "..BBCCBCCBYYBB..",
        "..BBCCBCCBYYBB..",
        "..BBBBBBBBBBBB..",
        "..BBDDBCCBCCBB..",
        "..BBDDBCCBCCBB..",
        "..BBBBBBBBBBBB..",
        "..BBCCBOOBCCBB..",
        "..BBCCBOOBCCBB..",
        "..BBBBBOOBBBBB..",
        "LLLLLLLLLLLLLLLL",
        "................",
    ]
    static let colors: [Character: UInt32] = [
        "G": 0xf0c040, "R": 0x8a4a5a, "B": 0x4b5a8a, "C": 0x6ee7ff, "Y": 0xf0c040, "D": 0x1b1e2b, "O": 0xa0673d, "L": 0x3a3e63,
    ]
    /// Без плашки офис стоит прямо на строке меню: вывеска и пол средних тонов, чтобы не пропадали ни на тёмной, ни на светлой.
    static let menuColors = colors.merging(["G": 0xb87d10, "L": 0x6f7496]) { $1 }

    static func color(_ hex: UInt32, _ alpha: CGFloat = 1) -> NSColor {
        NSColor(srgbRed: CGFloat(hex >> 16 & 0xff) / 255, green: CGFloat(hex >> 8 & 0xff) / 255, blue: CGFloat(hex & 0xff) / 255, alpha: alpha)
    }

    /// Клетки картинки в прямоугольнике; y сверху вниз, как в строках.
    static func draw(in rect: NSRect, fill: (Character) -> NSColor?) {
        let n = CGFloat(office.count)
        let px = min(rect.width, rect.height) / n
        for (y, row) in office.enumerated() {
            for (x, ch) in row.enumerated() {
                guard let c = fill(ch) else { continue }
                c.setFill()
                NSRect(x: rect.minX + CGFloat(x) * px, y: rect.maxY - CGFloat(y + 1) * px, width: px, height: px).fill()
            }
        }
    }

    /// Значок строки меню: тот же офис в цвете, но без плашки иконки приложения.
    static func menuIcon() -> NSImage {
        let img = NSImage(size: NSSize(width: 16, height: 16), flipped: false) { r in
            NSGraphicsContext.current?.shouldAntialias = false
            draw(in: r) { ch in menuColors[ch].map { color($0) } }
            return true
        }
        img.accessibilityDescription = "Agent Office"
        return img
    }

    /// Иконка приложения: офис ночью на тёмной плашке, светятся окна и вывеска.
    static func appIcon(_ side: Int) -> NSBitmapImageRep {
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: side, pixelsHigh: side, bitsPerSample: 8, samplesPerPixel: 4,
                                   hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        NSGraphicsContext.current?.imageInterpolation = .none
        let s = CGFloat(side)
        // плашка по сетке Big Sur: 824 из 1024, скругление ~185
        let plate = NSRect(x: s * 100 / 1024, y: s * 100 / 1024, width: s * 824 / 1024, height: s * 824 / 1024)
        let shape = NSBezierPath(roundedRect: plate, xRadius: s * 185 / 1024, yRadius: s * 185 / 1024)
        NSGradient(starting: color(0x2e3150), ending: color(0x14151f))!.draw(in: shape, angle: -90)
        NSGraphicsContext.current?.saveGraphicsState()
        shape.addClip()
        // звёзды и пол
        for (x, y) in [(0.22, 0.80), (0.78, 0.84), (0.70, 0.70), (0.30, 0.66), (0.86, 0.60)] {
            color(0xf4f1e8, 0.7).setFill()
            NSRect(x: plate.minX + plate.width * x, y: plate.minY + plate.height * y, width: s / 64, height: s / 64).fill()
        }
        // целые пиксели картинки, иначе между клетками видны швы
        let cell = (plate.width * 0.76 / CGFloat(office.count)).rounded(.down)
        let side = cell * CGFloat(office.count)
        let art = NSRect(x: (s - side) / 2, y: (s - side) / 2, width: side, height: side).integral
        // свет окон — мягкое пятно под зданием
        NSGradient(starting: color(0x6ee7ff, 0.22), ending: color(0x6ee7ff, 0))!.draw(in: NSBezierPath(ovalIn: art.insetBy(dx: -art.width * 0.1, dy: -art.height * 0.05)), relativeCenterPosition: .zero)
        NSGraphicsContext.current?.shouldAntialias = false
        draw(in: art) { ch in colors[ch].map { color($0) } }
        NSGraphicsContext.current?.shouldAntialias = true
        NSGraphicsContext.current?.restoreGraphicsState()
        color(0xf0c040, 0.55).setStroke()
        shape.lineWidth = max(1, s / 128)
        shape.stroke()
        NSGraphicsContext.restoreGraphicsState()
        return rep
    }

    /// scripts/build-menubar.sh: AgentOffice --render-icon <dir.iconset>
    static func renderIconset(_ dir: String) throws {
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        for base in [16, 32, 128, 256, 512] {
            for scale in [1, 2] {
                let name = "icon_\(base)x\(base)" + (scale == 2 ? "@2x" : "") + ".png"
                let png = appIcon(base * scale).representation(using: .png, properties: [:])!
                try png.write(to: URL(fileURLWithPath: dir).appendingPathComponent(name))
            }
        }
    }

    /// Лампочка «в работе» для строки меню.
    static func lamp(_ on: Bool, font: NSFont) -> NSAttributedString {
        let d: CGFloat = 7
        let img = NSImage(size: NSSize(width: d, height: d), flipped: false) { r in
            color(0x168f44, on ? 1 : 0.35).setFill()
            NSBezierPath(ovalIn: r).fill()
            return true
        }
        let a = NSTextAttachment()
        a.image = img
        a.bounds = NSRect(x: 0, y: (font.capHeight - d) / 2, width: d, height: d)
        return NSAttributedString(attachment: a)
    }
}

/// Подложка окошка: цвет --bg из web/menubar.html.
final class PopoverBackground: NSView {
    override func draw(_ dirtyRect: NSRect) {
        Pixel.color(0x14151f).setFill()
        dirtyRect.fill()
    }
}

// ── Сервер: свой дочерний процесс или чужой, уже слушающий порт ──────────────
final class Server {
    let cfg: Config
    private(set) var child: Process?
    private(set) var failures = 0        // падения подряд; после MAX_FAILURES не поднимаем
    var onChange: (() -> Void)?
    private var restarting = false
    private var quitting = false
    static let maxFailures = 10

    init(_ cfg: Config) { self.cfg = cfg }

    var gaveUp: Bool { failures >= Server.maxFailures }
    var isOurs: Bool { child?.isRunning ?? false }

    func spawn() {
        guard child == nil || !(child!.isRunning) else { return }
        guard !gaveUp else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: cfg.python)
        p.arguments = [cfg.server, "--port", String(cfg.port), "--watch-parent"]
        if let fh = openLog() {
            p.standardOutput = fh
            p.standardError = fh
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async { self?.died(proc) }
        }
        do {
            try p.run()
            child = p
            NSLog("Agent Office: server started, pid %d", p.processIdentifier)
        } catch {
            NSLog("Agent Office: cannot start server: %@", error.localizedDescription)
            failures += 1
        }
        onChange?()
    }

    private func died(_ proc: Process) {
        guard proc === child else { return }
        child = nil
        if quitting { return }
        if restarting {
            restarting = false
            spawn()
            return
        }
        failures += 1
        NSLog("Agent Office: server exited (%d), failure %d", proc.terminationStatus, failures)
        onChange?()
        if !gaveUp {
            DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in self?.spawn() }
        }
    }

    /// Свой сервер перезапускаем. Остался прежний экземпляр — гасим и поднимаем заново. Чужой процесс на порту не трогаем.
    func restart() {
        failures = 0
        if let c = child, c.isRunning {
            restarting = true
            c.terminate()
            return
        }
        for pid in listeners() where isOurServer(pid) { kill(pid, SIGTERM) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.spawn() }
    }

    func stop() {
        quitting = true
        guard let c = child, c.isRunning else { return }
        c.terminate()
        c.waitUntilExit()   // порт свободен к моменту, когда нас сменит другой инстанс
    }

    private func listeners() -> [pid_t] {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        p.arguments = ["-tiTCP:\(cfg.port)", "-sTCP:LISTEN"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return [] }
        p.waitUntilExit()
        let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        return text.split(whereSeparator: { $0.isNewline }).compactMap { pid_t($0) }
    }

    private func isOurServer(_ pid: pid_t) -> Bool {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/ps")
        p.arguments = ["-ww", "-o", "command=", "-p", String(pid)]   // иначе ps обрежет длинный путь к python
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return false }
        let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        p.waitUntilExit()
        return text.contains(cfg.server)
    }

    private func openLog() -> FileHandle? {
        let fm = FileManager.default
        if !fm.fileExists(atPath: cfg.log) { fm.createFile(atPath: cfg.log, contents: nil) }
        guard let fh = FileHandle(forWritingAtPath: cfg.log) else { return nil }
        fh.seekToEndOfFile()
        return fh
    }
}

// ── Автозапуск: тот же launchd-агент, что ставит scripts/install-launchd.sh ──
enum Autostart {
    static let label = "local.agent-office"
    static var plist: String { NSHomeDirectory() + "/Library/LaunchAgents/\(label).plist" }
    static var enabled: Bool { FileManager.default.fileExists(atPath: plist) }

    static func enable(log: String) throws {
        let exe = Bundle.main.bundlePath + "/Contents/MacOS/AgentOffice"
        let path = ProcessInfo.processInfo.environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        let xml = """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
          <key>Label</key><string>\(label)</string>
          <key>ProgramArguments</key>
          <array><string>\(exe)</string></array>
          <key>EnvironmentVariables</key>
          <dict><key>PATH</key><string>\(path)</string></dict>
          <key>RunAtLoad</key><true/>
          <key>KeepAlive</key>
          <dict><key>SuccessfulExit</key><false/></dict>
          <key>ThrottleInterval</key><integer>30</integer>
          <key>StandardOutPath</key><string>\(log)</string>
          <key>StandardErrorPath</key><string>\(log)</string>
        </dict>
        </plist>

        """
        try FileManager.default.createDirectory(atPath: (plist as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
        try xml.write(toFile: plist, atomically: true, encoding: .utf8)
        launchctl(["enable", "gui/\(getuid())/\(label)"])
    }

    /// Снимаем только файл: bootout убил бы нас, если мы и есть этот job. Вступает в силу при следующем входе.
    static func disable() throws {
        try FileManager.default.removeItem(atPath: plist)
        launchctl(["disable", "gui/\(getuid())/\(label)"])
    }

    /// Запущены ли мы самим launchd: в `launchctl print` у job'а наш pid.
    static var managed: Bool {
        let me = "pid = \(ProcessInfo.processInfo.processIdentifier)"
        return launchctlOutput(["print", "gui/\(getuid())/\(label)"]).contains(me)
    }

    @discardableResult
    static func launchctl(_ args: [String]) -> Int32 {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = args
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return -1 }
        p.waitUntilExit()
        return p.terminationStatus
    }

    static func launchctlOutput(_ args: [String]) -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = args
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return "" }
        p.waitUntilExit()
        return String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
    }
}

// ── Приложение ──────────────────────────────────────────────────────────────
final class App: NSObject, NSApplicationDelegate, NSMenuDelegate, NSPopoverDelegate, WKScriptMessageHandler, WKNavigationDelegate {
    let cfg = Config.load()
    lazy var server = Server(cfg)
    var item: NSStatusItem!
    let menu = NSMenu()          // правый клик: служебное
    let popover = NSPopover()    // левый клик: сессии и статистика
    var web: WKWebView!
    var pageAt = Date.distantPast  // когда страница грузилась: старую перечитываем, чтобы подхватить новый вид
    var offlinePage = false
    var sessions: [Session] = []
    var online = false
    var misses = 0              // опросов подряд без ответа
    var blink = true

    func applicationDidFinishLaunching(_ note: Notification) {
        // второй инстанс не нужен; но предыдущий может как раз уступать нам место (передача в launchd) — ждём до 3 с
        if let me = Bundle.main.bundleIdentifier {
            let mine = ProcessInfo.processInfo.processIdentifier
            let others = { NSRunningApplication.runningApplications(withBundleIdentifier: me).filter { $0.processIdentifier != mine } }
            var waited = 0
            while !others().isEmpty && waited < 30 { usleep(100_000); waited += 1 }
            if !others().isEmpty {
                NSLog("Agent Office: already running, exiting")
                NSApp.terminate(nil)
                return
            }
        }
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let b = item.button {
            b.image = Pixel.menuIcon()
            b.imagePosition = .imageLeading
            b.target = self
            b.action = #selector(clicked(_:))
            b.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        menu.delegate = self
        setupPopover()
        server.onChange = { [weak self] in self?.refreshTitle() }
        refreshTitle()

        probe { [weak self] ok in
            guard let self = self else { return }
            if ok { NSLog("Agent Office: server already on port %d", self.cfg.port) } else { self.server.spawn() }
        }
        let t = Timer(timeInterval: 5, repeats: true) { [weak self] _ in self?.poll() }
        RunLoop.main.add(t, forMode: .common)
        let b = Timer(timeInterval: 0.6, repeats: true) { [weak self] _ in self?.tick() }
        RunLoop.main.add(b, forMode: .common)
        poll()
    }

    func applicationWillTerminate(_ note: Notification) { server.stop() }

    // ── Опрос сервера ──
    private func request(_ path: String, _ done: @escaping (Data?) -> Void) {
        var req = URLRequest(url: URL(string: cfg.base + path)!)
        req.timeoutInterval = 3
        req.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async { done(ok ? data : nil) }
        }.resume()
    }

    private func probe(_ done: @escaping (Bool) -> Void) {
        request("/api/sessions") { done($0 != nil) }
    }

    func poll() {
        request("/api/sessions") { [weak self] data in
            guard let self = self else { return }
            if let d = data, let list = try? JSONDecoder().decode([Session].self, from: d) {
                self.sessions = list
                self.online = true
                self.misses = 0
                if self.offlinePage && self.popover.isShown { self.loadPage() }
            } else {
                self.online = false
                self.misses += 1
                // сервер молчит, а своего нет — поднимаем (чужой умер или не было никого)
                if self.misses >= 3 && !self.server.isOurs { self.server.spawn() }
            }
            self.refreshTitle()
        }
    }

    var today: Double { sessions.reduce(0) { $0 + ($1.today ?? 0) } }
    /// Сессии, где сейчас кто-то работает: руководитель или агенты.
    var busy: Int { sessions.filter { $0.working > 0 }.count }

    func tick() {
        guard online && busy > 0 else { return }
        blink.toggle()
        refreshTitle()
    }

    func refreshTitle() {
        guard let b = item?.button else { return }
        let font = NSFont.monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
        let title = NSMutableAttributedString(string: " " + (online ? money(today) : "$—"), attributes: [.font: font])
        if online && busy > 0 {
            title.append(NSAttributedString(string: "  ", attributes: [.font: font]))
            title.append(Pixel.lamp(blink, font: font))
            title.append(NSAttributedString(string: " \(busy)", attributes: [.font: font]))
        }
        b.attributedTitle = title
        b.toolTip = online ? "Сегодня \(money(today)) · сессий в работе: \(busy)" : "Сервер не отвечает"
    }

    // ── Клик: левый — окошко, правый — меню ──
    @objc func clicked(_ sender: NSStatusBarButton) {
        let right = NSApp.currentEvent?.type == .rightMouseUp || NSApp.currentEvent?.modifierFlags.contains(.control) == true
        if right {
            popover.performClose(nil)
            item.menu = menu
            sender.performClick(nil)
            item.menu = nil
            return
        }
        if popover.isShown {
            popover.performClose(nil)
            return
        }
        if Date().timeIntervalSince(pageAt) > 30 || offlinePage { loadPage() } else { show() }
        popover.show(relativeTo: sender.bounds, of: sender, preferredEdge: .minY)
        paintPopover()
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Рамка и хвостик окошка системные: под ними фон страницы, иначе вокруг неё серая кайма.
    func paintPopover() {
        guard let frame = web.window?.contentView?.superview,
              !frame.subviews.contains(where: { $0 is PopoverBackground }) else { return }
        let back = PopoverBackground(frame: frame.bounds)
        back.autoresizingMask = [.width, .height]
        frame.addSubview(back, positioned: .below, relativeTo: nil)
    }

    func setupPopover() {
        let conf = WKWebViewConfiguration()
        conf.userContentController.add(self, name: "ao")
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: 380, height: 520), configuration: conf)
        web.navigationDelegate = self
        web.setValue(false, forKey: "drawsBackground")
        let vc = NSViewController()
        vc.view = web
        popover.contentViewController = vc
        popover.contentSize = NSSize(width: 380, height: 520)
        popover.behavior = .transient
        popover.animates = false
        popover.appearance = NSAppearance(named: .darkAqua)
        popover.delegate = self
        loadPage()
    }

    func loadPage() {
        offlinePage = false
        pageAt = Date()
        web.load(URLRequest(url: URL(string: cfg.base + "/menubar.html")!, cachePolicy: .reloadIgnoringLocalCacheData))
    }

    /// Сервер не ответил — своя страничка с теми же кнопками.
    func loadOffline() {
        offlinePage = true
        let why = server.gaveUp ? "Сервер не запускается — загляните в лог." : "Сервер не отвечает, поднимаю…"
        web.loadHTMLString("""
        <body style="margin:0;background:#14151f;color:#e8e6f0;font:13px -apple-system;padding:18px 16px">
        <div style="color:#f0c040;font-weight:700;letter-spacing:.12em;font-size:12px">AGENT OFFICE</div>
        <p>\(why)</p>
        <p>\(["restart:Перезапустить сервер", "log:Лог", "quit:Выход"].map { p -> String in
            let kv = p.split(separator: ":")
            return "<button onclick=\"webkit.messageHandlers.ao.postMessage({cmd:'\(kv[0])'})\" style=\"background:#1c1d2b;color:#e8e6f0;border:0;padding:6px 10px;margin-right:4px\">\(kv[1])</button>"
        }.joined())</p></body>
        """, baseURL: nil)
        popover.contentSize = NSSize(width: 380, height: 150)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { loadOffline() }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { loadOffline() }

    /// Страница ожила или окошко открылось: свежие данные и состояние автозапуска.
    func show() {
        web.evaluateJavaScript("window.aoShow && aoShow({autostart: \(Autostart.enabled)})")
    }

    func popoverDidClose(_ notification: Notification) {
        web.evaluateJavaScript("window.aoHide && aoHide()")
    }

    func userContentController(_ uc: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let cmd = body["cmd"] as? String else { return }
        switch cmd {
        case "ready": show()
        case "size":
            let h = (body["arg"] as? NSNumber)?.doubleValue ?? 520
            let screen = NSScreen.main?.visibleFrame.height ?? 800
            popover.contentSize = NSSize(width: 380, height: min(max(h, 200), min(680, screen - 60)))
        case "open":
            popover.performClose(nil)
            open(body["arg"] as? String ?? "/")
        case "restart": restartServer()
        case "log": showLog()
        case "autostart":
            toggleAutostart()
            show()
        case "quit": quit()
        default: break
        }
    }

    // ── Меню по правому клику: служебное ──
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        if online {
            menu.addItem(label("Сегодня \(money(today)) · в работе: \(busy)"))
        } else if server.gaveUp {
            menu.addItem(label("Сервер не запускается, см. лог"))
        } else {
            menu.addItem(label("Сервер не отвечает"))
        }
        menu.addItem(.separator())
        menu.addItem(action("Открыть офис", #selector(openOffice), key: "o"))
        menu.addItem(action("Перезапустить сервер", #selector(restartServer), key: "r"))
        menu.addItem(action("Показать лог", #selector(showLog), key: "l"))
        menu.addItem(.separator())
        let auto = action("Автозапуск при входе", #selector(toggleAutostart), key: "")
        auto.state = Autostart.enabled ? .on : .off
        menu.addItem(auto)
        menu.addItem(action("Выход", #selector(quit), key: "q"))
    }

    private func label(_ title: String) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        i.isEnabled = false
        return i
    }

    private func action(_ title: String, _ sel: Selector, key: String) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: sel, keyEquivalent: key)
        i.target = self
        return i
    }

    // ── Действия ──
    private func open(_ path: String) {
        guard path.hasPrefix("/") else { return }   // иначе path допишется к имени хоста: localhost:8767@чужой.сайт
        if let u = URL(string: "http://localhost:\(cfg.port)" + path) { NSWorkspace.shared.open(u) }
    }

    @objc func openOffice() { open("/") }

    @objc func restartServer() {
        server.restart()
        misses = 0
    }

    @objc func showLog() {
        NSWorkspace.shared.open(URL(fileURLWithPath: cfg.log))
    }

    @objc func quit() { NSApp.terminate(nil) }

    @objc func toggleAutostart() {
        do {
            if Autostart.enabled {
                try Autostart.disable()
                return
            }
            try Autostart.enable(log: cfg.log)
            if Autostart.managed { return }
            // мы запущены вручную: отдаём место копии под launchd, чтобы она и поднимала нас после падений
            server.stop()
            Autostart.launchctl(["bootout", "gui/\(getuid())/\(Autostart.label)"])
            Autostart.launchctl(["bootstrap", "gui/\(getuid())", Autostart.plist])
            NSApp.terminate(nil)
        } catch {
            NSLog("Agent Office: autostart toggle failed: %@", error.localizedDescription)
            let a = NSAlert()
            a.messageText = "Не удалось изменить автозапуск"
            a.informativeText = error.localizedDescription
            a.runModal()
        }
    }
}

if let i = CommandLine.arguments.firstIndex(of: "--render-icon"), i + 1 < CommandLine.arguments.count {
    do {
        try Pixel.renderIconset(CommandLine.arguments[i + 1])
        exit(0)
    } catch {
        FileHandle.standardError.write("render-icon: \(error)\n".data(using: .utf8)!)
        exit(1)
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
