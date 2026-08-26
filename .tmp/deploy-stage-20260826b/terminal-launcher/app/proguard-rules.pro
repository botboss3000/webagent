# Termux terminal classes are reached partly via JNI; keep them intact.
-keep class com.termux.terminal.** { *; }
-keep class com.termux.view.** { *; }

# kotlinx.serialization generated serializers.
-keepattributes *Annotation*, InnerClasses
-keep,includedescriptorclasses class com.webagent.terminallauncher.**$$serializer { *; }
-keepclassmembers class com.webagent.terminallauncher.** {
    *** Companion;
}
-keepclasseswithmembers class com.webagent.terminallauncher.** {
    kotlinx.serialization.KSerializer serializer(...);
}
