#!/usr/bin/env python3
"""Run the production Java consent flow against deterministic SDK facades.

Only the native completion declaration is replaced with a recorder. This checks
ordering and request lifetimes without an Android device or JNI library.
"""
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap

SOURCE = (Path(sys.argv[1]) if len(sys.argv) > 1 else
          Path(__file__).resolve().parents[1] / "ump/src/java/UMPExtension.java")

STUBS = {
    "android/content/Context.java": """
        package android.content;
        public class Context {}
    """,
    "android/app/Activity.java": """
        package android.app;
        public class Activity extends android.content.Context {
            private static final Thread uiThread = Thread.currentThread();
            private static final java.util.Queue<Runnable> pending = new java.util.concurrent.ConcurrentLinkedQueue<>();
            public void runOnUiThread(Runnable task) {
                if (Thread.currentThread() == uiThread) task.run();
                else pending.add(task);
            }
            public static void requireUiThread() {
                if (Thread.currentThread() != uiThread) throw new AssertionError("SDK call outside UI thread");
            }
            public static void drain() {
                requireUiThread();
                Runnable task;
                while ((task = pending.poll()) != null) task.run();
            }
            public android.content.Context getApplicationContext() { return this; }
        }
    """,
    "android/content/SharedPreferences.java": """
        package android.content;
        public interface SharedPreferences { java.util.Map<String, ?> getAll(); }
    """,
    "android/preference/PreferenceManager.java": """
        package android.preference;
        public class PreferenceManager {
            public static android.content.SharedPreferences getDefaultSharedPreferences(android.content.Context context) {
                return () -> java.util.Collections.emptyMap();
            }
        }
    """,
    "android/util/Log.java": """
        package android.util;
        public class Log {
            public static int d(String tag, String message) { return 0; }
            public static int e(String tag, String message) { return 0; }
        }
    """,
    "com/google/android/ump/FormError.java": """
        package com.google.android.ump;
        public class FormError {
            private final int code;
            private final String message;
            public FormError() { this(2, "test failure"); }
            public FormError(int code, String message) { this.code = code; this.message = message; }
            public int getErrorCode() { return code; }
            public String getMessage() { return message; }
        }
    """,
    "com/google/android/ump/ConsentDebugSettings.java": """
        package com.google.android.ump;
        public class ConsentDebugSettings {
            public static class DebugGeography { public static final int DEBUG_GEOGRAPHY_EEA = 1; }
            public static class Builder {
                public Builder(android.app.Activity activity) {}
                public Builder setDebugGeography(int value) { return this; }
                public Builder addTestDeviceHashedId(String value) { return this; }
                public ConsentDebugSettings build() { return new ConsentDebugSettings(); }
            }
        }
    """,
    "com/google/android/ump/ConsentRequestParameters.java": """
        package com.google.android.ump;
        public class ConsentRequestParameters {
            public static class Builder {
                public Builder setConsentDebugSettings(ConsentDebugSettings value) { return this; }
                public ConsentRequestParameters build() { return new ConsentRequestParameters(); }
            }
        }
    """,
    "com/google/android/ump/ConsentInformation.java": """
        package com.google.android.ump;
        public class ConsentInformation {
            public static class ConsentStatus { public static final int REQUIRED = 2; }
            public enum PrivacyOptionsRequirementStatus { NOT_REQUIRED, REQUIRED }
            public interface OnConsentInfoUpdateSuccessListener { void onConsentInfoUpdateSuccess(); }
            public interface OnConsentInfoUpdateFailureListener { void onConsentInfoUpdateFailure(FormError error); }
            public int status;
            public boolean allowed;
            public OnConsentInfoUpdateSuccessListener success;
            public OnConsentInfoUpdateFailureListener failure;
            public Runnable onStatusRead;
            public int updates;
            public RuntimeException requestException;
            public void requestConsentInfoUpdate(android.app.Activity activity, ConsentRequestParameters params,
                    OnConsentInfoUpdateSuccessListener onSuccess, OnConsentInfoUpdateFailureListener onFailure) {
                android.app.Activity.requireUiThread();
                if (requestException != null) throw requestException;
                updates++;
                success = onSuccess; failure = onFailure;
            }
            public int getConsentStatus() {
                if (onStatusRead != null) {
                    Runnable hook = onStatusRead; onStatusRead = null; hook.run();
                }
                return status;
            }
            public boolean canRequestAds() { return allowed; }
            public PrivacyOptionsRequirementStatus getPrivacyOptionsRequirementStatus() {
                return PrivacyOptionsRequirementStatus.REQUIRED;
            }
            public void reset() { status = 0; allowed = false; }
        }
    """,
    "com/google/android/ump/UserMessagingPlatform.java": """
        package com.google.android.ump;
        public class UserMessagingPlatform {
            public interface DismissedListener { void dismissed(FormError error); }
            public static ConsentInformation info;
            public static DismissedListener form;
            public static DismissedListener privacy;
            public static int formsShown;
            public static int privacyShown;
            public static RuntimeException formException;
            public static RuntimeException privacyException;
            public static ConsentInformation getConsentInformation(android.app.Activity activity) { return info; }
            public static void loadAndShowConsentFormIfRequired(android.app.Activity activity, DismissedListener callback) {
                android.app.Activity.requireUiThread();
                if (formException != null) throw formException;
                if (info.status != ConsentInformation.ConsentStatus.REQUIRED) { callback.dismissed(null); return; }
                formsShown++; form = callback;
            }
            public static void showPrivacyOptionsForm(android.app.Activity activity, DismissedListener callback) {
                android.app.Activity.requireUiThread();
                if (privacyException != null) throw privacyException;
                privacyShown++; privacy = callback;
            }
        }
    """,
    "com/defold/umpext/TestConsentFlow.java": """
        package com.defold.umpext;
        import android.app.Activity;
        import com.google.android.ump.*;
        import java.util.ArrayList;
        import java.util.List;
        import java.util.concurrent.CountDownLatch;
        import java.util.concurrent.TimeUnit;

        public class TestConsentFlow {
            static final Activity activity = new Activity();
            static final List<String> events = new ArrayList<>();
            static final List<Integer> codes = new ArrayList<>();
            static final List<String> messages = new ArrayList<>();
            static int passed;
            static void record(int operation, int id, boolean success, int errorCode, String errorMessage) {
                events.add(operation + "/" + id + "/" + success);
                codes.add(errorCode); messages.add(errorMessage);
                check(success == (errorCode == 0));
                if (success) check(errorMessage == null);
            }
            static void check(boolean condition) { if (!condition) throw new AssertionError(events.toString()); }
            static void expect(String... expected) { check(events.equals(java.util.Arrays.asList(expected))); }
            static void reset() throws Exception {
                Activity.drain();
                var field = UMPExtension.class.getDeclaredField("consentInformation");
                field.setAccessible(true); field.set(null, null);
                field = UMPExtension.class.getDeclaredField("formStarted");
                field.setAccessible(true); field.setBoolean(null, false);
                field = UMPExtension.class.getDeclaredField("privacyOptionsState");
                field.setAccessible(true); field.setInt(null, 0);
                check(UMPExtension.resetConsentInformation());
                UserMessagingPlatform.info = new ConsentInformation();
                UserMessagingPlatform.formsShown = 0;
                UserMessagingPlatform.privacyShown = 0;
                UserMessagingPlatform.formException = null;
                UserMessagingPlatform.privacyException = null;
                UserMessagingPlatform.form = null;
                UserMessagingPlatform.privacy = null;
                events.clear(); codes.clear(); messages.clear();
            }
            static void update(int id, boolean deferred, boolean required) {
                check(UMPExtension.requestConsentInfoUpdate(activity, false, "", id, deferred));
                check(UMPExtension.getRequestState() == 0);
                UserMessagingPlatform.info.status = required ? 2 : 1;
                UserMessagingPlatform.info.success.onConsentInfoUpdateSuccess();
            }
            static void pass(String name) { passed++; System.out.println("PASS " + name); }
            static void background(Runnable task) throws Exception {
                var error = new java.util.concurrent.atomic.AtomicReference<Throwable>();
                var worker = new Thread(() -> {
                    try { task.run(); } catch (Throwable failure) { error.set(failure); }
                });
                worker.start(); worker.join(5000);
                check(!worker.isAlive());
                if (error.get() != null) throw new AssertionError(error.get());
            }
            public static void main(String[] args) throws Exception {
                reset(); update(1, true, true);
                expect("3/1/true");
                check(UMPExtension.getRequestState() == 3 && UMPExtension.wasConsentFormRequired());
                check(UserMessagingPlatform.formsShown == 0);
                check(UMPExtension.showConsentFormIfRequired(activity, 1));
                check(!UMPExtension.showConsentFormIfRequired(activity, 1));
                check(UserMessagingPlatform.formsShown == 1);
                expect("3/1/true");
                UserMessagingPlatform.form.dismissed(null);
                expect("3/1/true", "1/1/true");
                check(UMPExtension.getRequestState() == 1);
                pass("deferred required form waits for resume and opens once");

                reset(); update(2, true, false);
                expect("3/2/true"); check(!UMPExtension.wasConsentFormRequired());
                UMPExtension.showConsentFormIfRequired(activity, 2);
                check(UserMessagingPlatform.formsShown == 0 && UMPExtension.getRequestState() == 1);
                expect("3/2/true", "1/2/true");
                pass("deferred no-form flow completes without a window");

                reset(); update(3, false, true);
                expect(); check(UserMessagingPlatform.formsShown == 1);
                UserMessagingPlatform.form.dismissed(null);
                expect("1/3/true");
                pass("automatic required-form flow preserves its completion callback");

                reset(); update(4, false, false);
                check(UserMessagingPlatform.formsShown == 0);
                expect("1/4/true");
                pass("automatic no-form flow remains compatible");

                for (boolean deferred : new boolean[]{true, false}) {
                    reset(); UMPExtension.requestConsentInfoUpdate(activity, false, "", 5, deferred);
                    UserMessagingPlatform.info.failure.onConsentInfoUpdateFailure(new FormError());
                    check(UMPExtension.getRequestState() == 2 && UserMessagingPlatform.formsShown == 0);
                    expect((deferred ? "3" : "1") + "/5/false");
                    check(codes.get(0) == 2 && messages.get(0).equals("test failure"));
                    pass((deferred ? "deferred" : "automatic") + " information failure completes without a form");
                }

                reset(); update(6, true, true);
                UMPExtension.showConsentFormIfRequired(activity, 6);
                UserMessagingPlatform.form.dismissed(new FormError());
                expect("3/6/true", "1/6/false"); check(UMPExtension.getRequestState() == 2);
                pass("deferred form failure completes with failure");

                reset(); UMPExtension.requestConsentInfoUpdate(activity, false, "", 7, true);
                var oldUpdate = UserMessagingPlatform.info.success;
                UMPExtension.requestConsentInfoUpdate(activity, false, "", 8, true);
                UserMessagingPlatform.info.status = 2;
                oldUpdate.onConsentInfoUpdateSuccess(); expect(); check(UMPExtension.getRequestState() == 0);
                UserMessagingPlatform.info.success.onConsentInfoUpdateSuccess(); expect("3/8/true");
                UMPExtension.showConsentFormIfRequired(activity, 7);
                check(UserMessagingPlatform.formsShown == 0);
                UMPExtension.showConsentFormIfRequired(activity, 8);
                check(UserMessagingPlatform.formsShown == 1);
                pass("superseded update and resume are ignored");

                reset(); update(9, true, true);
                UMPExtension.showConsentFormIfRequired(activity, 9);
                var oldForm = UserMessagingPlatform.form;
                check(!UMPExtension.requestConsentInfoUpdate(activity, false, "", 10, true));
                check(!UMPExtension.showPrivacyOptionsForm(activity, 10));
                check(!UMPExtension.resetConsentInformation());
                check(UserMessagingPlatform.info.updates == 1 && UserMessagingPlatform.formsShown == 1);
                oldForm.dismissed(null);
                expect("3/9/true", "1/9/true"); check(UMPExtension.getRequestState() == 1);
                check(UMPExtension.requestConsentInfoUpdate(activity, false, "", 10, true));
                oldForm.dismissed(null);
                expect("3/9/true", "1/9/true"); check(UMPExtension.getRequestState() == 0);
                UserMessagingPlatform.info.success.onConsentInfoUpdateSuccess();
                expect("3/9/true", "1/9/true", "3/10/true");
                pass("active consent form rejects overlapping work and keeps its completion");

                reset(); update(11, true, false);
                UMPExtension.showConsentFormIfRequired(activity, 11);
                UMPExtension.showPrivacyOptionsForm(activity, 12);
                check(UMPExtension.getPrivacyOptionsState() == 1);
                UserMessagingPlatform.privacy.dismissed(null);
                expect("3/11/true", "1/11/true", "2/12/true");
                check(UMPExtension.getPrivacyOptionsState() == 2);
                pass("privacy options retain their separate callback");

                reset(); update(13, false, false);
                check(UMPExtension.showPrivacyOptionsForm(activity, 14));
                var firstPrivacy = UserMessagingPlatform.privacy;
                check(!UMPExtension.showPrivacyOptionsForm(activity, 15));
                check(!UMPExtension.requestConsentInfoUpdate(activity, false, "", 16, false));
                check(!UMPExtension.resetConsentInformation());
                check(UserMessagingPlatform.privacyShown == 1);
                firstPrivacy.dismissed(null);
                expect("1/13/true", "2/14/true");
                check(UMPExtension.getPrivacyOptionsState() == 2);
                check(UMPExtension.showPrivacyOptionsForm(activity, 15));
                firstPrivacy.dismissed(null);
                check(UMPExtension.getPrivacyOptionsState() == 1);
                UserMessagingPlatform.privacy.dismissed(new FormError(4, "privacy unavailable"));
                expect("1/13/true", "2/14/true", "2/15/false");
                check(codes.get(2) == 4 && messages.get(2).equals("privacy unavailable"));
                pass("privacy duplicates preserve the first form and stale dismissals are ignored");

                reset(); update(17, true, true);
                background(() -> {
                    check(UMPExtension.showConsentFormIfRequired(activity, 17));
                    check(!UMPExtension.showConsentFormIfRequired(activity, 17));
                    check(!UMPExtension.requestConsentInfoUpdate(activity, false, "", 18, false));
                });
                check(UserMessagingPlatform.formsShown == 0);
                Activity.drain(); check(UserMessagingPlatform.formsShown == 1);
                UserMessagingPlatform.form.dismissed(null);
                expect("3/17/true", "1/17/true");
                pass("consent form is reserved before queued UI work runs");

                reset(); update(19, false, false);
                background(() -> {
                    check(UMPExtension.showPrivacyOptionsForm(activity, 20));
                    check(!UMPExtension.showPrivacyOptionsForm(activity, 21));
                });
                check(UserMessagingPlatform.privacyShown == 0);
                Activity.drain(); check(UserMessagingPlatform.privacyShown == 1);
                UserMessagingPlatform.privacy.dismissed(null);
                expect("1/19/true", "2/20/true");
                pass("privacy form is reserved before queued UI work runs");

                reset();
                background(() -> {
                    check(UMPExtension.requestConsentInfoUpdate(activity, false, "", 22, true));
                    check(UMPExtension.requestConsentInfoUpdate(activity, false, "", 23, true));
                });
                check(UserMessagingPlatform.info.updates == 0);
                Activity.drain(); check(UserMessagingPlatform.info.updates == 1);
                UserMessagingPlatform.info.status = 1;
                UserMessagingPlatform.info.success.onConsentInfoUpdateSuccess();
                expect("3/23/true");
                pass("superseded queued update is skipped on the UI thread");

                reset(); UMPExtension.requestConsentInfoUpdate(activity, false, "", 24, true);
                var queuedOldSuccess = UserMessagingPlatform.info.success;
                background(queuedOldSuccess::onConsentInfoUpdateSuccess);
                check(UMPExtension.requestConsentInfoUpdate(activity, false, "", 25, true));
                Activity.drain(); expect(); check(UMPExtension.getRequestState() == 0);
                UserMessagingPlatform.info.status = 1;
                UserMessagingPlatform.info.success.onConsentInfoUpdateSuccess();
                expect("3/25/true");
                pass("off-thread completion rechecks its ID after reaching the UI thread");

                reset(); UMPExtension.requestConsentInfoUpdate(activity, false, "", 26, true);
                UserMessagingPlatform.info.status = 2;
                var oldSuccess = UserMessagingPlatform.info.success;
                var attempted = new CountDownLatch(1);
                var returned = new CountDownLatch(1);
                var accepted = new java.util.concurrent.atomic.AtomicBoolean();
                var gameThread = new Thread(() -> {
                    attempted.countDown();
                    accepted.set(UMPExtension.requestConsentInfoUpdate(activity, false, "", 27, true));
                    returned.countDown();
                });
                UserMessagingPlatform.info.onStatusRead = () -> {
                    gameThread.start();
                    try {
                        check(attempted.await(5, TimeUnit.SECONDS));
                        returned.await(250, TimeUnit.MILLISECONDS);
                    } catch (InterruptedException error) { throw new AssertionError(error); }
                };
                oldSuccess.onConsentInfoUpdateSuccess();
                gameThread.join(5000); check(!gameThread.isAlive() && accepted.get());
                check(UMPExtension.getRequestState() == 0 && !UMPExtension.wasConsentFormRequired());
                Activity.drain();
                UserMessagingPlatform.info.success.onConsentInfoUpdateSuccess();
                expect("3/26/true", "3/27/true");
                pass("concurrent update cannot be overwritten midway through an old success callback");

                reset(); UMPExtension.requestConsentInfoUpdate(activity, false, "", 28, true);
                var beforeReset = UserMessagingPlatform.info.success;
                check(UMPExtension.resetConsentInformation());
                beforeReset.onConsentInfoUpdateSuccess();
                expect(); check(UMPExtension.getRequestState() == 0 && !UMPExtension.wasConsentFormRequired());
                update(29, true, true);
                check(UMPExtension.resetConsentInformation());
                check(!UMPExtension.showConsentFormIfRequired(activity, 29));
                check(!UMPExtension.wasConsentFormRequired() && UMPExtension.getPrivacyOptionsState() == 0);
                pass("reset clears diagnostic state and invalidates pending callbacks and resumes");

                reset(); UserMessagingPlatform.info.requestException = new IllegalStateException("bad request");
                check(UMPExtension.requestConsentInfoUpdate(activity, false, "", 30, true));
                expect("3/30/false");
                check(codes.get(0) == -1 && messages.get(0).contains("bad request"));
                check(UMPExtension.getRequestState() == 2);
                pass("synchronous SDK request exception produces a detailed failure");

                reset(); UserMessagingPlatform.formException = new IllegalStateException("bad form");
                update(31, false, true);
                expect("1/31/false");
                check(codes.get(0) == -1 && messages.get(0).contains("bad form"));
                check(UMPExtension.resetConsentInformation());
                pass("synchronous form exception releases its reservation");

                reset(); update(32, false, false);
                UserMessagingPlatform.privacyException = new IllegalStateException("bad privacy");
                check(UMPExtension.showPrivacyOptionsForm(activity, 33));
                expect("1/32/true", "2/33/false");
                check(codes.get(1) == -1 && messages.get(1).contains("bad privacy"));
                check(UMPExtension.getPrivacyOptionsState() == 3 && UMPExtension.resetConsentInformation());
                pass("synchronous privacy exception releases its reservation");
                System.out.println(passed + " Java consent flow checks passed");
            }
        }
    """,
}

declaration = "private static native void onNativeCompletion(int operation, int requestId, boolean success, int errorCode, String errorMessage);"
production = SOURCE.read_text()
assert production.count(declaration) == 1
production = production.replace(declaration, """private static void onNativeCompletion(int operation, int requestId, boolean success, int errorCode, String errorMessage) {
    TestConsentFlow.record(operation, requestId, success, errorCode, errorMessage);
}""")

with tempfile.TemporaryDirectory(prefix="ump-consent-") as folder:
    root = Path(folder)
    sources = dict(STUBS)
    sources["com/defold/umpext/UMPExtension.java"] = production
    for name, source in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source))
    subprocess.run(["javac", "-d", str(root / "classes"),
                    *[str(root / name) for name in sources]], check=True)
    subprocess.run(["java", "-cp", str(root / "classes"), "com.defold.umpext.TestConsentFlow"], check=True)
    descriptors = subprocess.run(
        ["javap", "-s", "-p", "-classpath", str(root / "classes"), "com.defold.umpext.UMPExtension"],
        check=True, capture_output=True, text=True).stdout
    java_methods = dict(re.findall(r"\b(\w+)\([^;\n]*\);\s+descriptor: (\S+)", descriptors))
    project = Path(__file__).resolve().parents[1]
    native = (project / "ump/src/extension.cpp").read_text()
    bindings = re.findall(r'\{"(\w+)", "([^"]+)", 0\}', native)
    for name, signature in bindings:
        assert java_methods.get(name) == signature, (name, signature, java_methods.get(name))
    assert java_methods["onNativeCompletion"] == "(IIZILjava/lang/String;)V"
    exports = re.findall(r'\{"([a-z_]+)", \w+\}', native)
    api = (project / "ump/api/ump.script_api").read_text()
    documented = re.findall(r"^    - name: ([a-z_]+)$", api, re.M)
    assert len(exports) == len(set(exports)) and set(exports) == set(documented)
    constants = dict(re.findall(r"^    ((?:REQUEST_STATE_|PRIVACY_OPTIONS_STATE_|GDPR_APPLIES_|ERROR_)\w+) = (-?\d+),", native, re.M))
    java_constants = dict(re.findall(r"private static final int ((?:REQUEST_STATE_|PRIVACY_OPTIONS_STATE_|GDPR_APPLIES_|ERROR_)\w+) = (-?\d+);", production))
    assert constants == java_constants
    assert "initialize_mobile_ads_sdk" not in exports
    print(f"PASS JNI signatures, {len(exports)} unique documented functions and {len(constants)} shared constants")
