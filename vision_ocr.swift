import Foundation
import Vision
import AppKit

let args = CommandLine.arguments
if args.count < 2 {
    fputs("usage: vision_ocr.swift [--jsonl] <image-path>\n", stderr)
    exit(1)
}

let jsonMode = args.contains("--jsonl")
let imagePath = args.last!
let imageURL = URL(fileURLWithPath: imagePath)

guard let image = NSImage(contentsOf: imageURL) else {
    fputs("failed to load image\n", stderr)
    exit(2)
}

guard let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    fputs("failed to build cgImage\n", stderr)
    exit(3)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["ja-JP", "zh-Hans", "en-US"]

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])

do {
    try handler.perform([request])
    let observations = (request.results ?? []) as [VNRecognizedTextObservation]
    for observation in observations {
        if let candidate = observation.topCandidates(1).first {
            if jsonMode {
                let box = observation.boundingBox
                let payload: [String: Any] = [
                    "text": candidate.string,
                    "confidence": candidate.confidence,
                    "x": box.origin.x,
                    "y": box.origin.y,
                    "width": box.size.width,
                    "height": box.size.height,
                ]
                let data = try JSONSerialization.data(withJSONObject: payload, options: [])
                if let line = String(data: data, encoding: .utf8) {
                    print(line)
                }
            } else {
                print(candidate.string)
            }
        }
    }
} catch {
    fputs("OCR failed: \(error)\n", stderr)
    exit(4)
}