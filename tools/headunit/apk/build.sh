#!/usr/bin/env bash
# build.sh — build the Prius GNSS logger APK without Gradle. NOT RUN YET: the P52 lacks the
# toolchain. Install first (Ubuntu 26.04 apt, a few hundred MB; ask the owner):
#   sudo apt install openjdk-21-jdk-headless aapt dalvik-exchange apksigner \
#        google-android-platform-27-installer   # (downloads android.jar API 27 from Google)
#   # or android-sdk-platform-23 (API 23 jar): then the API 26 calls in GnssService
#   # (getSpeedAccuracy..., startForegroundService, NotificationChannel) must go via reflection.
# Output: build/prius-gnss.apk, signed with a local debug key (build/debug.keystore,
# created on first run; not secret, but kept out of git).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
B="$HERE/build"
JAR="${ANDROID_JAR:-/usr/lib/android-sdk/platforms/android-27/android.jar}"
[ -f "$JAR" ] || { echo "android.jar not found at $JAR (set ANDROID_JAR)" >&2; exit 1; }
for t in javac aapt dx apksigner keytool; do command -v $t >/dev/null || { echo "missing $t" >&2; exit 1; }; done
rm -rf "$B/classes" "$B"/*.apk; mkdir -p "$B/classes"
aapt package -f -M "$HERE/AndroidManifest.xml" -I "$JAR" -F "$B/unsigned.apk"
javac --release 8 -classpath "$JAR" -d "$B/classes" $(find "$HERE/src" -name '*.java')
dx --dex --output="$B/classes.dex" "$B/classes"
(cd "$B" && aapt add unsigned.apk classes.dex)
[ -f "$B/debug.keystore" ] || keytool -genkeypair -keystore "$B/debug.keystore" -storepass android \
    -keypass android -alias debug -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=Prius GNSS debug"
apksigner sign --ks "$B/debug.keystore" --ks-pass pass:android --out "$B/prius-gnss.apk" "$B/unsigned.apk"
echo "$B/prius-gnss.apk"
# Install (on site):  hu-push --write build/prius-gnss.apk && hu-shell pm install -r /data/local/tmp/prius-gnss.apk
#                     hu-shell pm grant pl.topa.priusgnss android.permission.ACCESS_FINE_LOCATION
#                     hu-shell am startforegroundservice -n pl.topa.priusgnss/.GnssService
