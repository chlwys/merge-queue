// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "MergeQueueMenu",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "MergeQueueMenu", targets: ["MergeQueueMenu"])],
    targets: [
        .target(name: "QueueModel"),
        .executableTarget(name: "MergeQueueMenu", dependencies: ["QueueModel"]),
        .testTarget(name: "QueueModelTests", dependencies: ["QueueModel"]),
        .testTarget(name: "QueueRuntimeTests", dependencies: ["MergeQueueMenu"])
    ]
)
