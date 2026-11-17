#!/usr/bin/env bash
# build.sh — build the Prius agent APK without Gradle (needs: openjdk-21-jdk-headless aapt
# dalvik-exchange apksigner and the API 27 android.jar; all on the P52 since 2026-11-17).
# Output: build/prius-agent.apk, signed with a local debug key (build/debug.keystore,
# created on first run; not secret, but kept out of git).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
B="$HERE/build"
JAR="${ANDROID_JAR:-/usr/lib/android-sdk/platforms/android-27/android.jar}"
[ -f "$JAR" ] || { echo "android.jar not found at $JAR (set ANDROID_JAR)" >&2; exit 1; }
# Debian/Ubuntu ship dx only under build-tools (dalvik-exchange package)
for d in /usr/lib/android-sdk/build-tools/*/; do [ -x "$d/dx" ] && PATH="$PATH:$d"; done
for t in javac aapt dx apksigner keytool; do command -v $t >/dev/null || { echo "missing $t" >&2; exit 1; }; done
rm -rf "$B/classes" "$B"/*.apk; mkdir -p "$B/classes"
aapt package -f -M "$HERE/AndroidManifest.xml" -I "$JAR" -F "$B/unsigned.apk"
javac --release 8 -classpath "$JAR" -d "$B/classes" $(find "$HERE/src" -name '*.java')
dx --dex --output="$B/classes.dex" "$B/classes"
(cd "$B" && aapt add unsigned.apk classes.dex)
[ -f "$B/debug.keystore" ] || keytool -genkeypair -keystore "$B/debug.keystore" -storepass android \
    -keypass android -alias debug -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=Prius agent debug"
apksigner sign --ks "$B/debug.keystore" --ks-pass pass:android --out "$B/prius-agent.apk" "$B/unsigned.apk"
echo "$B/prius-agent.apk"
# Install: adb install -r build/prius-agent.apk
#          adb shell cmd activity start-foreground-service -n pl.topa.priusagent/.AlertService --es tone 880:150,0:80,880:150
