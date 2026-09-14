// Renders the app icon: a macOS-style rounded square with a warm gradient and
// the SF Symbol "books.vertical.fill". Usage: swift make-icon.swift <out.iconset>
import AppKit

let outDir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.iconset"
try? FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)

func render(canvas: CGFloat) -> NSImage {
    let img = NSImage(size: NSSize(width: canvas, height: canvas))
    img.lockFocus()
    NSGraphicsContext.current?.imageInterpolation = .high
    // Apple's icon grid: the tile fills ~80% of the canvas.
    let inset = canvas * 0.10
    let tile = NSRect(x: inset, y: inset, width: canvas - inset * 2, height: canvas - inset * 2)
    let radius = tile.width * 0.2237
    let path = NSBezierPath(roundedRect: tile, xRadius: radius, yRadius: radius)

    let shadow = NSShadow()
    shadow.shadowColor = NSColor.black.withAlphaComponent(0.28)
    shadow.shadowBlurRadius = canvas * 0.02
    shadow.shadowOffset = NSSize(width: 0, height: -canvas * 0.008)
    NSGraphicsContext.saveGraphicsState()
    shadow.set()
    NSColor(calibratedRed: 0.80, green: 0.44, blue: 0.31, alpha: 1).setFill()
    path.fill()
    NSGraphicsContext.restoreGraphicsState()

    let gradient = NSGradient(colors: [
        NSColor(calibratedRed: 0.93, green: 0.63, blue: 0.50, alpha: 1),
        NSColor(calibratedRed: 0.80, green: 0.42, blue: 0.27, alpha: 1),
    ])!
    gradient.draw(in: path, angle: -60)

    // Subtle top highlight.
    NSGraphicsContext.saveGraphicsState()
    path.addClip()
    let hl = NSGradient(colors: [NSColor.white.withAlphaComponent(0.22), NSColor.white.withAlphaComponent(0)])!
    hl.draw(in: NSRect(x: tile.minX, y: tile.midY, width: tile.width, height: tile.height / 2), angle: 90)
    NSGraphicsContext.restoreGraphicsState()

    let cfg = NSImage.SymbolConfiguration(pointSize: tile.width * 0.50, weight: .medium)
    if let symbol = NSImage(systemSymbolName: "books.vertical.fill", accessibilityDescription: nil)?
        .withSymbolConfiguration(cfg) {
        let tinted = NSImage(size: symbol.size)
        tinted.lockFocus()
        symbol.draw(in: NSRect(origin: .zero, size: symbol.size))
        NSColor.white.set()
        NSRect(origin: .zero, size: symbol.size).fill(using: .sourceAtop)
        tinted.unlockFocus()
        let s = tinted.size
        let scale = min(tile.width * 0.56 / s.width, tile.height * 0.56 / s.height)
        let w = s.width * scale, h = s.height * scale
        let rect = NSRect(x: tile.midX - w / 2, y: tile.midY - h / 2 - tile.height * 0.01, width: w, height: h)
        let symShadow = NSShadow()
        symShadow.shadowColor = NSColor.black.withAlphaComponent(0.18)
        symShadow.shadowBlurRadius = canvas * 0.01
        symShadow.shadowOffset = NSSize(width: 0, height: -canvas * 0.006)
        NSGraphicsContext.saveGraphicsState()
        symShadow.set()
        tinted.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 1)
        NSGraphicsContext.restoreGraphicsState()
    }
    img.unlockFocus()
    return img
}

func writePNG(_ image: NSImage, pixels: Int, to path: String) {
    guard let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff) else { return }
    let out = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                               colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    out.size = NSSize(width: pixels, height: pixels)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: out)
    NSGraphicsContext.current?.imageInterpolation = .high
    rep.draw(in: NSRect(x: 0, y: 0, width: pixels, height: pixels))
    NSGraphicsContext.restoreGraphicsState()
    try? out.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
}

let master = render(canvas: 1024)
let sizes: [(String, Int)] = [
    ("icon_16x16", 16), ("icon_16x16@2x", 32), ("icon_32x32", 32), ("icon_32x32@2x", 64),
    ("icon_128x128", 128), ("icon_128x128@2x", 256), ("icon_256x256", 256), ("icon_256x256@2x", 512),
    ("icon_512x512", 512), ("icon_512x512@2x", 1024),
]
for (name, px) in sizes {
    writePNG(master, pixels: px, to: "\(outDir)/\(name).png")
}
print("wrote \(sizes.count) icons to \(outDir)")
