pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // Termux terminal-emulator / terminal-view are published through JitPack.
        maven { url = uri("https://jitpack.io") }
    }
}

rootProject.name = "TerminalLauncher"
include(":app")
