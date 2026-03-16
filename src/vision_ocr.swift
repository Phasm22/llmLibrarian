import CoreGraphics
import Foundation
import ImageIO
import Vision

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(Data("usage: vision_ocr <image-path>\n".utf8))
    exit(2)
}

let imagePath = CommandLine.arguments[1]
let imageURL = URL(fileURLWithPath: imagePath)

guard
    let imageSource = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
    let image = CGImageSourceCreateImageAtIndex(imageSource, 0, nil)
else {
    FileHandle.standardError.write(Data("failed to load image\n".utf8))
    exit(1)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true

let handler = VNImageRequestHandler(cgImage: image, options: [:])
do {
    try handler.perform([request])
    let observations = (request.results ?? []).sorted { lhs, rhs in
        let yDiff = lhs.boundingBox.minY - rhs.boundingBox.minY
        if abs(yDiff) > 0.01 {
            return lhs.boundingBox.minY > rhs.boundingBox.minY
        }
        return lhs.boundingBox.minX < rhs.boundingBox.minX
    }

    let lines = observations.compactMap { observation -> String? in
        guard let candidate = observation.topCandidates(1).first else {
            return nil
        }
        let text = candidate.string.trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? nil : text
    }

    // Build structured observations with bbox for column-aware reconstruction
    var obs_array: [[String: Any]] = []
    for observation in observations {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let text = candidate.string.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty { continue }
        let bb = observation.boundingBox
        obs_array.append([
            "text": text,
            "x": Double(bb.minX),
            "y": Double(bb.minY),
            "w": Double(bb.width),
            "h": Double(bb.height),
            "confidence": Double(candidate.confidence),
        ])
    }

    let payload: [String: Any] = [
        "text": lines.joined(separator: "\n"),
        "observations": obs_array,
    ]
    let data = try JSONSerialization.data(withJSONObject: payload, options: [])
    FileHandle.standardOutput.write(data)
} catch {
    FileHandle.standardError.write(Data("\(error)\n".utf8))
    exit(1)
}
