package pl.topa.priusgnss;

// Prius GNSS logger for the head unit (Android 8.1, API 27). SKELETON, NOT BUILT OR RUN YET.
//
// Keeps the GPS provider active and writes every fix to logcat (tag PriusGnss, one JSON
// object per line, already in the backend's fix format, docs/POSITION_FEEDER.md §4).
// The car computer reads it with `hu-gnss-bridge --source logcat --post`, so the backend
// token never lands on the head unit and the app needs no network permission.
//
// Start:  adb shell am startforegroundservice -n pl.topa.priusgnss/.GnssService
//         (the app also starts on BOOT_COMPLETED via BootReceiver)
// Stop:   adb shell am stopservice -n pl.topa.priusgnss/.GnssService
// Grant:  adb shell pm grant pl.topa.priusgnss android.permission.ACCESS_FINE_LOCATION

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Bundle;
import android.os.IBinder;
import android.os.SystemClock;
import android.util.Log;

public class GnssService extends Service implements LocationListener {
    static final String TAG = "PriusGnss";
    private LocationManager lm;

    @Override public IBinder onBind(Intent i) { return null; }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        nm.createNotificationChannel(new NotificationChannel("gnss", "GNSS", NotificationManager.IMPORTANCE_LOW));
        Notification n = new Notification.Builder(this, "gnss")
                .setContentTitle("Prius GNSS").setSmallIcon(android.R.drawable.ic_menu_mylocation).build();
        startForeground(1, n);
        if (lm == null) {
            lm = (LocationManager) getSystemService(LOCATION_SERVICE);
            try {
                lm.requestLocationUpdates(LocationManager.GPS_PROVIDER, 0L, 0f, this);
                Log.i(TAG, "{\"event\":\"started\"}");
            } catch (SecurityException e) {
                Log.e(TAG, "{\"event\":\"no_permission\"}");
            }
        }
        return START_STICKY;
    }

    @Override public void onDestroy() {
        if (lm != null) lm.removeUpdates(this);
        super.onDestroy();
    }

    @Override public void onLocationChanged(Location l) {
        long ageMs = (SystemClock.elapsedRealtimeNanos() - l.getElapsedRealtimeNanos()) / 1000000L;
        StringBuilder s = new StringBuilder(256);
        s.append("{\"lat\":").append(l.getLatitude())
         .append(",\"lon\":").append(l.getLongitude())
         .append(",\"age_ms\":").append(ageMs)
         .append(",\"utc_ms\":").append(l.getTime())
         .append(",\"et_ms\":").append(l.getElapsedRealtimeNanos() / 1000000L);
        if (l.hasAccuracy()) s.append(",\"hacc_m\":").append(l.getAccuracy());
        if (l.hasSpeed()) s.append(",\"speed_mps\":").append(l.getSpeed());
        if (l.hasBearing()) s.append(",\"course_deg\":").append(l.getBearing());
        if (l.hasAltitude()) s.append(",\"alt_m\":").append(l.getAltitude());
        if (l.hasSpeedAccuracy()) s.append(",\"speed_acc_mps\":").append(l.getSpeedAccuracyMetersPerSecond());
        if (l.hasBearingAccuracy()) s.append(",\"course_acc_deg\":").append(l.getBearingAccuracyDegrees());
        if (l.hasVerticalAccuracy()) s.append(",\"vacc_m\":").append(l.getVerticalAccuracyMeters());
        Bundle x = l.getExtras();
        if (x != null && x.containsKey("satellites")) s.append(",\"sats\":").append(x.getInt("satellites"));
        s.append('}');
        Log.i(TAG, s.toString());
    }

    @Override public void onProviderDisabled(String p) { Log.i(TAG, "{\"fix\":\"none\",\"event\":\"provider_disabled\"}"); }
    @Override public void onProviderEnabled(String p) { Log.i(TAG, "{\"event\":\"provider_enabled\"}"); }
    @Override public void onStatusChanged(String p, int st, Bundle b) { }
}
