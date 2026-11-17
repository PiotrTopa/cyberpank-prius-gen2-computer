package pl.topa.priusgnss;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

// Starts GnssService at boot. SKELETON, NOT BUILT OR RUN YET. The unit's QuickBoot power
// cycle (autochips.intent.action.QB_POWERON) may need to be added once it is seen in logcat.
public class BootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context c, Intent i) {
        c.startForegroundService(new Intent(c, GnssService.class));
    }
}
