// swift-tools-version:5.9
// The Mac Utils setup app. Built by scripts/build-app.sh into build/Mac Utils.app.
import PackageDescription

let package = Package(
    name: "MacUtilsSetup",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "MacUtilsSetup", path: "Sources/MacUtilsSetup")
    ]
)
