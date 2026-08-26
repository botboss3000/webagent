plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.serialization")
}

android {
    namespace = "com.webagent.terminallauncher"
    compileSdk = 34

    defaultConfig {
        // FORK BUILD: the applicationId MUST be com.termux so the app's private
        // data dir is /data/data/com.termux/files -- the exact path the prebuilt
        // Termux bootstrap binaries have baked into their shebangs and rpaths.
        // (Code/namespace stays com.webagent.terminallauncher; only the install
        // id is com.termux.) This is why the fork replaces a stock Termux.
        applicationId = "com.termux"
        minSdk = 24
        // MUST stay 28: on targetSdk 29+ Android's SELinux policy forbids exec()
        // of files in the app's writable home dir, which would break every Termux
        // binary. This is the same reason Termux targets 28 and ships via F-Droid.
        targetSdk = 28
        versionCode = 1
        versionName = "1.0.0"

        // Termux's native terminal-emulator ships .so for these ABIs.
        ndk {
            abiFilters += listOf("arm64-v8a", "armeabi-v7a", "x86", "x86_64")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.recyclerview:recyclerview:1.3.2")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.3")
    implementation("androidx.activity:activity-ktx:1.9.0")

    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.6.3")

    // ---------------------------------------------------------------------
    // Real PTY-backed terminal, courtesy of the Termux project (Apache-2.0).
    //   terminal-emulator -> TerminalSession + the native forkpty subprocess
    //   terminal-view     -> TerminalView (the scrolling, selectable widget)
    //
    // These are resolved from JitPack (see settings.gradle.kts). The tag
    // below is a real Termux release; if it fails to resolve, open
    // https://jitpack.io/#termux/termux-app and pick the latest green build,
    // then update BOTH coordinates to the same tag.
    // ---------------------------------------------------------------------
    implementation("com.github.termux.termux-app:terminal-view:v0.118.0")
    implementation("com.github.termux.termux-app:terminal-emulator:v0.118.0")
}
