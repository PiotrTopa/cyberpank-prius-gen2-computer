package pl.topa.priusagent;

// Prius agent for the head unit (Android 8.1, API 27): alert sounds on command.
//
// The car computer sends one intent per alert over adb (40 ms, no JVM start):
//   cmd activity start-foreground-service -n pl.topa.priusagent/.AlertService \
//       --es tone 880:150,0:80,880:150 [--ef gain 0.8] [--es say "text"] [--es lang pl] \
//       [--es file /sdcard/x.ogg] [--es usage nav|alarm|media] [--es id abc] [--ez stop true]
// tone = comma list of frequency_hz:duration_ms, frequency 0 = silence. A tone and a
// text in one intent play in that order. Alerts queue; "stop" drops the queue.
// Every alert asks for transient audio focus (may duck) and gives it back when done.
// Result lines go to logcat, tag PriusAgent, one JSON object per line.

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.media.MediaPlayer;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.util.Log;

import java.util.Locale;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

public class AlertService extends Service {
    static final String TAG = "PriusAgent";
    static final int RATE = 44100;
    private HandlerThread thread;
    private Handler queue;
    private AudioManager am;
    private TextToSpeech tts;
    private volatile boolean ttsReady;
    private volatile AudioTrack track;
    private volatile MediaPlayer player;
    private volatile CountDownLatch waiting;

    @Override public IBinder onBind(Intent i) { return null; }

    @Override public void onCreate() {
        super.onCreate();
        am = (AudioManager) getSystemService(AUDIO_SERVICE);
        thread = new HandlerThread("alerts");
        thread.start();
        queue = new Handler(thread.getLooper());
        tts = new TextToSpeech(this, new TextToSpeech.OnInitListener() {
            @Override public void onInit(int status) {
                ttsReady = status == TextToSpeech.SUCCESS;
                Log.i(TAG, "{\"event\":\"tts\",\"ready\":" + ttsReady + "}");
            }
        });
        Log.i(TAG, "{\"event\":\"started\",\"version\":\"0.1\"}");
    }

    @Override public int onStartCommand(final Intent intent, int flags, int startId) {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        nm.createNotificationChannel(new NotificationChannel("agent", "Prius agent", NotificationManager.IMPORTANCE_MIN));
        startForeground(1, new Notification.Builder(this, "agent").setContentTitle("Prius agent")
                .setSmallIcon(android.R.drawable.ic_lock_silent_mode_off).build());
        if (intent == null) return START_STICKY;
        if (intent.getBooleanExtra("stop", false)) {
            queue.removeCallbacksAndMessages(null);
            stopNow();
            Log.i(TAG, "{\"event\":\"stopped\"}");
            return START_STICKY;
        }
        if (intent.hasExtra("tone") || intent.hasExtra("say") || intent.hasExtra("file")) {
            queue.post(new Runnable() { @Override public void run() { play(intent); } });
        }
        return START_STICKY;
    }

    @Override public void onDestroy() {
        stopNow();
        if (tts != null) tts.shutdown();
        thread.quitSafely();
        super.onDestroy();
    }

    private void stopNow() {
        try { AudioTrack t = track; if (t != null) t.stop(); } catch (Exception e) { }
        try { MediaPlayer p = player; if (p != null) p.stop(); } catch (Exception e) { }
        try { if (tts != null) tts.stop(); } catch (Exception e) { }
        CountDownLatch w = waiting; if (w != null) w.countDown();
    }

    private static int usage(String u) {
        if ("alarm".equals(u)) return AudioAttributes.USAGE_ALARM;
        if ("media".equals(u)) return AudioAttributes.USAGE_MEDIA;
        return AudioAttributes.USAGE_ASSISTANCE_NAVIGATION_GUIDANCE;
    }

    private void play(Intent in) {
        String id = in.getStringExtra("id");
        long t0 = android.os.SystemClock.elapsedRealtime();
        AudioAttributes attrs = new AudioAttributes.Builder().setUsage(usage(in.getStringExtra("usage")))
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build();
        AudioFocusRequest focus = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                .setAudioAttributes(attrs).build();
        int granted = am.requestAudioFocus(focus);
        String err = null;
        try {
            float gain = Math.max(0f, Math.min(1f, in.getFloatExtra("gain", 0.8f)));
            String tone = in.getStringExtra("tone");
            if (tone != null) tone(tone, gain, attrs);
            String file = in.getStringExtra("file");
            if (file != null) file(file, gain, attrs);
            String say = in.getStringExtra("say");
            if (say != null) say(say, in.getStringExtra("lang"), gain, attrs);
        } catch (Exception e) {
            err = String.valueOf(e);
        } finally {
            am.abandonAudioFocusRequest(focus);
        }
        Log.i(TAG, "{\"event\":\"alert\",\"id\":" + (id == null ? "null" : "\"" + id.replace("\"", "") + "\"")
                + ",\"ok\":" + (err == null) + ",\"focus\":" + (granted == AudioManager.AUDIOFOCUS_REQUEST_GRANTED)
                + ",\"ms\":" + (android.os.SystemClock.elapsedRealtime() - t0)
                + (err == null ? "" : ",\"error\":\"" + err.replace("\"", "'") + "\"") + "}");
    }

    /** "880:150,0:80,880:150" -> one static PCM buffer with 4 ms edges on every segment. */
    static short[] render(String spec, float gain) {
        String[] parts = spec.split(",");
        int[] f = new int[parts.length], n = new int[parts.length];
        int total = 0;
        for (int i = 0; i < parts.length; i++) {
            String[] kv = parts[i].trim().split(":");
            f[i] = Math.max(0, Math.min(8000, Integer.parseInt(kv[0].trim())));
            int ms = Math.max(1, Math.min(5000, Integer.parseInt(kv[1].trim())));
            n[i] = RATE * ms / 1000;
            total += n[i];
            if (total > RATE * 15) throw new IllegalArgumentException("tone longer than 15 s");
        }
        short[] pcm = new short[total + RATE / 50];          // 20 ms of silence at the end
        int edge = RATE * 4 / 1000, o = 0;
        for (int i = 0; i < parts.length; i++) {
            if (f[i] > 0) {
                double w = 2 * Math.PI * f[i] / RATE;
                for (int k = 0; k < n[i]; k++) {
                    double env = Math.min(1.0, Math.min(k, n[i] - 1 - k) / (double) edge);
                    pcm[o + k] = (short) (Math.sin(w * k) * env * gain * 32000);
                }
            }
            o += n[i];
        }
        return pcm;
    }

    private void tone(String spec, float gain, AudioAttributes attrs) throws Exception {
        short[] pcm = render(spec, gain);
        AudioTrack t = new AudioTrack.Builder().setAudioAttributes(attrs)
                .setAudioFormat(new AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(RATE).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                .setTransferMode(AudioTrack.MODE_STATIC).setBufferSizeInBytes(pcm.length * 2).build();
        track = t;
        try {
            t.write(pcm, 0, pcm.length);
            t.play();
            waiting = new CountDownLatch(1);
            waiting.await(pcm.length * 1000L / RATE + 60, TimeUnit.MILLISECONDS);
        } finally {
            track = null;
            try { t.stop(); } catch (Exception e) { }
            t.release();
        }
    }

    private void file(String path, float gain, AudioAttributes attrs) throws Exception {
        final CountDownLatch done = new CountDownLatch(1);
        MediaPlayer p = new MediaPlayer();
        player = p;
        try {
            p.setAudioAttributes(attrs);
            p.setDataSource(path);
            p.setVolume(gain, gain);
            p.setOnCompletionListener(new MediaPlayer.OnCompletionListener() {
                @Override public void onCompletion(MediaPlayer mp) { done.countDown(); }
            });
            p.prepare();
            p.start();
            waiting = done;
            done.await(Math.min(30000, p.getDuration() + 500), TimeUnit.MILLISECONDS);
        } finally {
            player = null;
            p.release();
        }
    }

    private void say(String text, String lang, float gain, AudioAttributes attrs) throws Exception {
        for (int i = 0; i < 40 && !ttsReady; i++) Thread.sleep(100);
        if (!ttsReady) throw new IllegalStateException("tts not ready");
        final CountDownLatch done = new CountDownLatch(1);
        tts.setAudioAttributes(attrs);
        if (lang != null) tts.setLanguage(Locale.forLanguageTag(lang));
        tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
            @Override public void onStart(String u) { }
            @Override public void onDone(String u) { done.countDown(); }
            @Override public void onError(String u) { done.countDown(); }
        });
        Bundle b = new Bundle();
        b.putFloat(TextToSpeech.Engine.KEY_PARAM_VOLUME, gain);
        if (tts.speak(text, TextToSpeech.QUEUE_FLUSH, b, "a") != TextToSpeech.SUCCESS)
            throw new IllegalStateException("tts speak failed");
        waiting = done;
        done.await(30, TimeUnit.SECONDS);
    }
}
