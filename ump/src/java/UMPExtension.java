package com.defold.umpext;

import android.app.Activity;
import android.content.SharedPreferences;
import android.preference.PreferenceManager;
import android.util.Log;

import com.google.android.ump.ConsentDebugSettings;
import com.google.android.ump.ConsentInformation;
import com.google.android.ump.ConsentInformation.PrivacyOptionsRequirementStatus;
import com.google.android.ump.ConsentRequestParameters;
import com.google.android.ump.FormError;
import com.google.android.ump.UserMessagingPlatform;

public class UMPExtension {
    private static final String TAG = "UMPExtension";
    private static final int REQUEST_CONSENT = 1;
    private static final int PRIVACY_OPTIONS = 2;
    private static final int CONSENT_INFO_UPDATE = 3;
    private static native void onNativeCompletion(int operation, int requestId, boolean success, int errorCode, String errorMessage);

    // Keep these values in sync with the Lua constants registered in extension.cpp.
    private static final int REQUEST_STATE_UPDATING = 0;
    private static final int REQUEST_STATE_COMPLETE = 1;
    private static final int REQUEST_STATE_FAILED = 2;
    private static final int REQUEST_STATE_FORM_PENDING = 3;
    private static final int PRIVACY_OPTIONS_STATE_NOT_SHOWN = 0;
    private static final int PRIVACY_OPTIONS_STATE_SHOWING = 1;
    private static final int PRIVACY_OPTIONS_STATE_DISMISSED = 2;
    private static final int PRIVACY_OPTIONS_STATE_ERROR = 3;
    private static final int GDPR_APPLIES_UNKNOWN = -1;
    private static final int GDPR_APPLIES_NO = 0;
    private static final int GDPR_APPLIES_YES = 1;
    private static final int ERROR_BRIDGE = -1;

    // All state transitions and reads share the class monitor. Requests, forms
    // and their callbacks also run on the UI thread.
    private static ConsentInformation consentInformation;
    private static int requestState = REQUEST_STATE_UPDATING;
    private static boolean requiredFormOnUpdate;
    private static boolean formStarted;
    private static int privacyOptionsState = PRIVACY_OPTIONS_STATE_NOT_SHOWN;
    private static int latestRequestId;
    private static int latestPrivacyId;

    private static void complete(int operation, int requestId, int errorCode, String errorMessage) {
        if (errorCode != 0) Log.e(TAG, "UMP operation failed (" + errorCode + "): " + errorMessage);
        onNativeCompletion(operation, requestId, errorCode == 0, errorCode, errorMessage);
    }

    private static void complete(int operation, int requestId, FormError error) {
        complete(operation, requestId, error == null ? 0 : error.getErrorCode(),
                error == null ? null : error.getMessage());
    }

    private static void failOperation(int operation, int requestId, RuntimeException error) {
        if (operation == PRIVACY_OPTIONS) privacyOptionsState = PRIVACY_OPTIONS_STATE_ERROR;
        else {
            requestState = REQUEST_STATE_FAILED;
            formStarted = false;
        }
        complete(operation, requestId, ERROR_BRIDGE, error.toString());
    }

    private static void dispatch(Activity activity, int operation, int requestId, Runnable action) {
        try {
            activity.runOnUiThread(() -> {
                synchronized (UMPExtension.class) {
                    int currentId = operation == PRIVACY_OPTIONS ? latestPrivacyId : latestRequestId;
                    if (requestId != currentId) return;
                    try { action.run(); }
                    catch (RuntimeException error) { failOperation(operation, requestId, error); }
                }
            });
        } catch (RuntimeException error) {
            synchronized (UMPExtension.class) {
                int currentId = operation == PRIVACY_OPTIONS ? latestPrivacyId : latestRequestId;
                if (requestId == currentId) failOperation(operation, requestId, error);
            }
        }
    }

    /** Return false while a consent or privacy-options form is loading/showing. */
    public static synchronized boolean requestConsentInfoUpdate(Activity activity, boolean testDevice,
            String testDeviceHashedId, int requestId, boolean deferForm) {
        if (formStarted || privacyOptionsState == PRIVACY_OPTIONS_STATE_SHOWING) return false;
        latestRequestId = requestId;
        requestState = REQUEST_STATE_UPDATING;
        requiredFormOnUpdate = false;
        int operation = deferForm ? CONSENT_INFO_UPDATE : REQUEST_CONSENT;
        dispatch(activity, operation, requestId, () -> {
            ConsentRequestParameters.Builder paramsBuilder = new ConsentRequestParameters.Builder();
            if (testDevice) {
                ConsentDebugSettings debugSettings = new ConsentDebugSettings.Builder(activity)
                        .setDebugGeography(ConsentDebugSettings.DebugGeography.DEBUG_GEOGRAPHY_EEA)
                        .addTestDeviceHashedId(testDeviceHashedId)
                        .build();
                paramsBuilder.setConsentDebugSettings(debugSettings);
            }
            if (consentInformation == null) consentInformation = UserMessagingPlatform.getConsentInformation(activity);
            consentInformation.requestConsentInfoUpdate(activity, paramsBuilder.build(),
                    () -> dispatch(activity, operation, requestId, () -> {
                        if (requestState != REQUEST_STATE_UPDATING) return;
                        requiredFormOnUpdate = consentInformation.getConsentStatus()
                                == ConsentInformation.ConsentStatus.REQUIRED;
                        requestState = REQUEST_STATE_FORM_PENDING;
                        if (deferForm) complete(CONSENT_INFO_UPDATE, requestId, null);
                        else showConsentFormIfRequired(activity, requestId);
                    }),
                    error -> dispatch(activity, operation, requestId, () -> {
                        if (requestState != REQUEST_STATE_UPDATING) return;
                        requestState = REQUEST_STATE_FAILED;
                        complete(operation, requestId, error);
                    }));
        });
        return true;
    }

    /** Reserve the form before scheduling UI work so repeated calls cannot overlap. */
    public static synchronized boolean showConsentFormIfRequired(Activity activity, int requestId) {
        if (requestId != latestRequestId || requestState != REQUEST_STATE_FORM_PENDING
                || formStarted || privacyOptionsState == PRIVACY_OPTIONS_STATE_SHOWING) return false;
        formStarted = true;
        dispatch(activity, REQUEST_CONSENT, requestId, () ->
                UserMessagingPlatform.loadAndShowConsentFormIfRequired(activity,
                        error -> dispatch(activity, REQUEST_CONSENT, requestId, () -> {
                            if (!formStarted) return;
                            formStarted = false;
                            requestState = error == null ? REQUEST_STATE_COMPLETE : REQUEST_STATE_FAILED;
                            complete(REQUEST_CONSENT, requestId, error);
                        })));
        return true;
    }

    public static synchronized int getRequestState() { return requestState; }
    public static synchronized boolean wasConsentFormRequired() { return requiredFormOnUpdate; }

    private static Object getPreference(Activity activity, String key) {
        SharedPreferences preferences = PreferenceManager.getDefaultSharedPreferences(activity.getApplicationContext());
        return preferences.getAll().get(key);
    }

    // Missing or unexpected regional data must not be treated as outside the EEA.
    public static int getGdprApplies(Activity activity) {
        Object value = getPreference(activity, "IABTCF_gdprApplies");
        if (value instanceof Integer && ((Integer) value == GDPR_APPLIES_NO || (Integer) value == GDPR_APPLIES_YES)) {
            return (Integer) value;
        }
        if ("0".equals(value) || "1".equals(value)) return Integer.parseInt((String) value);
        return GDPR_APPLIES_UNKNOWN;
    }

    public static String getPurposeConsents(Activity activity) {
        Object value = getPreference(activity, "IABTCF_PurposeConsents");
        return value instanceof String ? (String) value : null;
    }

    public static synchronized boolean isPrivacyOptionsRequired() {
        return consentInformation != null && consentInformation.getPrivacyOptionsRequirementStatus()
                == PrivacyOptionsRequirementStatus.REQUIRED;
    }

    /** Keep the current form and its completion alive when another call is rejected. */
    public static synchronized boolean showPrivacyOptionsForm(Activity activity, int requestId) {
        if (formStarted || privacyOptionsState == PRIVACY_OPTIONS_STATE_SHOWING
                || requestState == REQUEST_STATE_UPDATING || requestState == REQUEST_STATE_FORM_PENDING) return false;
        latestPrivacyId = requestId;
        privacyOptionsState = PRIVACY_OPTIONS_STATE_SHOWING;
        dispatch(activity, PRIVACY_OPTIONS, requestId, () ->
                UserMessagingPlatform.showPrivacyOptionsForm(activity,
                        error -> dispatch(activity, PRIVACY_OPTIONS, requestId, () -> {
                            if (privacyOptionsState != PRIVACY_OPTIONS_STATE_SHOWING) return;
                            privacyOptionsState = error == null
                                    ? PRIVACY_OPTIONS_STATE_DISMISSED : PRIVACY_OPTIONS_STATE_ERROR;
                            complete(PRIVACY_OPTIONS, requestId, error);
                        })));
        return true;
    }

    public static synchronized int getPrivacyOptionsState() { return privacyOptionsState; }
    public static synchronized boolean canRequestAds() {
        return consentInformation != null && consentInformation.canRequestAds();
    }

    /** Reset SDK and diagnostic state for testing, invalidating pending updates. */
    public static synchronized boolean resetConsentInformation() {
        if (formStarted || privacyOptionsState == PRIVACY_OPTIONS_STATE_SHOWING) return false;
        if (consentInformation != null) consentInformation.reset();
        latestRequestId = 0;
        latestPrivacyId = 0;
        requestState = REQUEST_STATE_UPDATING;
        requiredFormOnUpdate = false;
        privacyOptionsState = PRIVACY_OPTIONS_STATE_NOT_SHOWN;
        return true;
    }

    public static synchronized int getConsentStatus() {
        return consentInformation == null ? -1 : consentInformation.getConsentStatus();
    }
}
