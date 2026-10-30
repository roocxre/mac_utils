// Recognizes the text in an image and prints one JSON box per word.
// Usage: ocr <image path>. Coordinates are normalized with the origin at the bottom left.
// Release build (the installer's bin/ocr-helper; lib/ocr.py otherwise compiles it itself):
//   xcrun --sdk macosx swiftc -O -target arm64-apple-macos14.0 -o bin/ocr-helper lib/ocr.swift
import Cocoa
import Vision
import Foundation

struct BoxData: Codable {
    let text: String
    let x: Double
    let y: Double
    let w: Double
    let h: Double
}

guard CommandLine.arguments.count > 1 else {
    FileHandle.standardError.write("usage: ocr <image>\n".data(using: .utf8)!)
    exit(2)
}
let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard let image = NSImage(contentsOf: imageURL),
      let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("could not read image\n".data(using: .utf8)!)
    exit(1)
}

var results: [BoxData] = []

let request = VNRecognizeTextRequest { (request, error) in
    guard let observations = request.results as? [VNRecognizedTextObservation] else { return }
    for obs in observations {
        guard let candidate = obs.topCandidates(1).first else { continue }
        let fullStr = candidate.string

        // Subdivide each observation into words to get true per-word bounding boxes
        let words = fullStr.components(separatedBy: " ")
        var searchStart = fullStr.startIndex

        for word in words {
            if word.isEmpty { continue }
            if let range = fullStr.range(of: word, range: searchStart..<fullStr.endIndex) {
                searchStart = range.upperBound
                if let box = try? candidate.boundingBox(for: range) {
                    let normRect = box.boundingBox
                    results.append(BoxData(
                        text: word,
                        x: Double(normRect.origin.x),
                        y: Double(normRect.origin.y),
                        w: Double(normRect.size.width),
                        h: Double(normRect.size.height)
                    ))
                }
            }
        }
    }
}
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
} catch {
    FileHandle.standardError.write("recognition failed: \(error)\n".data(using: .utf8)!)
    exit(1)
}

if let data = try? JSONEncoder().encode(results),
   let jsonStr = String(data: data, encoding: .utf8) {
    print(jsonStr)
}
