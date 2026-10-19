import Foundation
import Vision
import ImageIO

struct Reply: Encodable {
    let supportedRevisions: [Int]
    let revision: Int?
    let observations: [Observation]
}
struct Observation: Encodable {
    let identifier: String
    let confidence: Float
}

func emit<T: Encodable>(_ value: T) throws {
    let data = try JSONEncoder().encode(value)
    print(String(data: data, encoding: .utf8)!)
}

let revisions = Array(VNClassifyImageRequest.supportedRevisions)
if CommandLine.arguments.count == 2 && CommandLine.arguments[1] == "--capabilities" {
    try emit(Reply(supportedRevisions: revisions, revision: nil, observations: []))
} else if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--identifiers" {
    let request = VNClassifyImageRequest()
    request.revision = Int(CommandLine.arguments[2])!
    let identifiers = try request.supportedIdentifiers()
    try emit(identifiers)
} else {
    guard CommandLine.arguments.count == 3,
          let revision = Int(CommandLine.arguments[1]), revisions.contains(revision) else {
        fputs("Unsupported Vision revision or invalid arguments\n", stderr)
        exit(2)
    }
    let request = VNClassifyImageRequest()
    request.revision = revision
    request.usesCPUOnly = true
    do {
        let url = URL(fileURLWithPath: CommandLine.arguments[2])
        let handler = VNImageRequestHandler(url: url, options: [:])
        try handler.perform([request])
        let observations = (request.results ?? []).prefix(40).map {
            Observation(identifier: $0.identifier, confidence: $0.confidence)
        }
        try emit(Reply(supportedRevisions: revisions, revision: revision, observations: observations))
    } catch {
        let failure = error as NSError
        fputs("Vision request failed (\(failure.domain), \(failure.code)): \(failure.localizedDescription)\n", stderr)
        exit(1)
    }
}
