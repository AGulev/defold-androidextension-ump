#define EXTENSION_NAME ump
#define LIB_NAME "ump"
#define MODULE_NAME LIB_NAME
#define DLIB_LOG_DOMAIN LIB_NAME
#include <dmsdk/sdk.h>

#if defined(DM_PLATFORM_ANDROID)
#include <dmsdk/dlib/android.h>
#include <stdlib.h>
#include <string.h>

enum Operation { REQUEST_CONSENT = 1, PRIVACY_OPTIONS = 2, CONSENT_INFO_UPDATE = 3 };

struct Completion {
    int m_Operation;
    int m_Id;
    bool m_Success;
    int m_ErrorCode;
    char* m_ErrorMessage;
};

static dmMutex::HMutex g_Mutex;
static dmArray<Completion> g_Completions;
static bool g_AcceptCompletions = false;
static int g_NextId = 0;
static int g_RequestId = 0;
static int g_PrivacyId = 0;
static dmScript::LuaCallbackInfo* g_RequestCallback = 0;
static dmScript::LuaCallbackInfo* g_InfoCallback = 0;
static dmScript::LuaCallbackInfo* g_PrivacyCallback = 0;

static void ClearCallback(dmScript::LuaCallbackInfo*& callback)
{
    if (callback) dmScript::DestroyCallback(callback);
    callback = 0;
}

extern "C" JNIEXPORT void JNICALL Java_com_defold_umpext_UMPExtension_onNativeCompletion(
        JNIEnv* env, jclass cls, jint operation, jint id, jboolean success, jint errorCode, jstring errorMessage)
{
    Completion completion = {(int)operation, (int)id, success == JNI_TRUE, (int)errorCode, 0};
    if (errorMessage) {
        const char* chars = env->GetStringUTFChars(errorMessage, 0);
        if (!chars) return;
        completion.m_ErrorMessage = strdup(chars);
        env->ReleaseStringUTFChars(errorMessage, chars);
    }
    DM_MUTEX_SCOPED_LOCK(g_Mutex);
    if (!g_AcceptCompletions) {
        free(completion.m_ErrorMessage);
        return;
    }
    if (g_Completions.Full()) g_Completions.OffsetCapacity(4);
    g_Completions.Push(completion);
}

static void InvokeCompletion(dmScript::LuaCallbackInfo* callback, const Completion& completion)
{
    if (!callback) return;
    if (dmScript::IsCallbackValid(callback) && dmScript::SetupCallback(callback)) {
        lua_State* L = dmScript::GetCallbackLuaContext(callback);
        lua_pushboolean(L, completion.m_Success);
        if (completion.m_Success) {
            lua_pushnil(L);
        } else {
            lua_createtable(L, 0, 2);
            lua_pushinteger(L, completion.m_ErrorCode);
            lua_setfield(L, -2, "code");
            lua_pushstring(L, completion.m_ErrorMessage ? completion.m_ErrorMessage : "UMP operation failed");
            lua_setfield(L, -2, "message");
        }
        dmScript::PCall(L, 3, 0);
        dmScript::TeardownCallback(callback);
    }
    dmScript::DestroyCallback(callback);
}

static dmExtension::Result UpdateExtension(dmExtension::Params* params)
{
    dmArray<Completion> completions;
    {
        DM_MUTEX_SCOPED_LOCK(g_Mutex);
        completions.Swap(g_Completions);
    }
    for (uint32_t i = 0; i < completions.Size(); ++i) {
        const Completion& completion = completions[i];
        dmScript::LuaCallbackInfo** slot = 0;
        if (completion.m_Operation == REQUEST_CONSENT && completion.m_Id == g_RequestId) {
            slot = &g_RequestCallback;
        } else if (completion.m_Operation == CONSENT_INFO_UPDATE && completion.m_Id == g_RequestId) {
            slot = &g_InfoCallback;
        } else if (completion.m_Operation == PRIVACY_OPTIONS && completion.m_Id == g_PrivacyId) {
            slot = &g_PrivacyCallback;
        }
        if (slot) {
            dmScript::LuaCallbackInfo* callback = *slot;
            *slot = 0;
            InvokeCompletion(callback, completion);
        }
        free(completion.m_ErrorMessage);
    }
    return dmExtension::RESULT_OK;
}

// Keep these values in sync with UMPExtension.java.
enum UmpConstant {
    REQUEST_STATE_UPDATING = 0,
    REQUEST_STATE_COMPLETE = 1,
    REQUEST_STATE_FAILED = 2,
    REQUEST_STATE_FORM_PENDING = 3,
    PRIVACY_OPTIONS_STATE_NOT_SHOWN = 0,
    PRIVACY_OPTIONS_STATE_SHOWING = 1,
    PRIVACY_OPTIONS_STATE_DISMISSED = 2,
    PRIVACY_OPTIONS_STATE_ERROR = 3,
    GDPR_APPLIES_UNKNOWN = -1,
    GDPR_APPLIES_NO = 0,
    GDPR_APPLIES_YES = 1,
    ERROR_BRIDGE = -1,
};

enum JavaMethod {
    JAVA_REQUEST_CONSENT_INFO_UPDATE,
    JAVA_SHOW_CONSENT_FORM_IF_REQUIRED,
    JAVA_SHOW_PRIVACY_OPTIONS_FORM,
    JAVA_IS_PRIVACY_OPTIONS_REQUIRED,
    JAVA_CAN_REQUEST_ADS,
    JAVA_RESET_CONSENT_INFORMATION,
    JAVA_GET_CONSENT_STATUS,
    JAVA_GET_REQUEST_STATE,
    JAVA_WAS_CONSENT_FORM_REQUIRED,
    JAVA_GET_PRIVACY_OPTIONS_STATE,
    JAVA_GET_GDPR_APPLIES,
    JAVA_GET_PURPOSE_CONSENTS,
    JAVA_METHOD_COUNT,
};

struct JavaBinding {
    const char* m_Name;
    const char* m_Signature;
    jmethodID m_Method;
};

static jclass g_JavaClass = 0;
static JavaBinding g_JavaMethods[] = {
    {"requestConsentInfoUpdate", "(Landroid/app/Activity;ZLjava/lang/String;IZ)Z", 0},
    {"showConsentFormIfRequired", "(Landroid/app/Activity;I)Z", 0},
    {"showPrivacyOptionsForm", "(Landroid/app/Activity;I)Z", 0},
    {"isPrivacyOptionsRequired", "()Z", 0},
    {"canRequestAds", "()Z", 0},
    {"resetConsentInformation", "()Z", 0},
    {"getConsentStatus", "()I", 0},
    {"getRequestState", "()I", 0},
    {"wasConsentFormRequired", "()Z", 0},
    {"getPrivacyOptionsState", "()I", 0},
    {"getGdprApplies", "(Landroid/app/Activity;)I", 0},
    {"getPurposeConsents", "(Landroid/app/Activity;)Ljava/lang/String;", 0},
};
static_assert(sizeof(g_JavaMethods) / sizeof(g_JavaMethods[0]) == JAVA_METHOD_COUNT, "Missing Java binding");

static bool CheckJavaException(JNIEnv* env, const char* operation)
{
    if (!env) {
        dmLogError("Cannot attach to Java for %s", operation);
        return false;
    }
    if (env->ExceptionCheck()) {
        dmLogError("Java call failed: %s", operation);
        env->ExceptionDescribe();
        env->ExceptionClear();
        return false;
    }
    return true;
}

static void ReleaseJavaBindings(JNIEnv* env)
{
    if (g_JavaClass && env) env->DeleteGlobalRef(g_JavaClass);
    g_JavaClass = 0;
    for (int i = 0; i < JAVA_METHOD_COUNT; ++i) g_JavaMethods[i].m_Method = 0;
}

static bool CacheJavaBindings(JNIEnv* env)
{
    if (!CheckJavaException(env, "UMP initialization")) return false;
    // LoadClass uses local references internally; release all of them together.
    if (env->PushLocalFrame(8) < 0) {
        CheckJavaException(env, "UMP class loader");
        return false;
    }
    jclass cls = dmAndroid::LoadClass(env, "com.defold.umpext.UMPExtension");
    bool ok = CheckJavaException(env, "UMP class loader") && cls;
    if (ok) {
        g_JavaClass = (jclass)env->NewGlobalRef(cls);
        ok = CheckJavaException(env, "UMP class reference") && g_JavaClass;
    }
    for (int i = 0; ok && i < JAVA_METHOD_COUNT; ++i) {
        JavaBinding& binding = g_JavaMethods[i];
        binding.m_Method = env->GetStaticMethodID(g_JavaClass, binding.m_Name, binding.m_Signature);
        ok = CheckJavaException(env, binding.m_Name) && binding.m_Method;
    }
    env->PopLocalFrame(0);
    if (!ok) ReleaseJavaBindings(env);
    return ok;
}

static bool JavaReady(JNIEnv* env, JavaMethod method)
{
    return CheckJavaException(env, g_JavaMethods[method].m_Name)
        && g_JavaClass && g_JavaMethods[method].m_Method;
}

static bool CallJavaBoolean(JavaMethod method, const jvalue* args, bool& value)
{
    dmAndroid::ThreadAttacher thread;
    JNIEnv* env = thread.GetEnv();
    if (!JavaReady(env, method)) return false;
    jboolean result = env->CallStaticBooleanMethodA(g_JavaClass, g_JavaMethods[method].m_Method, args);
    if (!CheckJavaException(env, g_JavaMethods[method].m_Name)) return false;
    value = result == JNI_TRUE;
    return true;
}

static bool CallJavaInt(JavaMethod method, const jvalue* args, int& value)
{
    dmAndroid::ThreadAttacher thread;
    JNIEnv* env = thread.GetEnv();
    if (!JavaReady(env, method)) return false;
    jint result = env->CallStaticIntMethodA(g_JavaClass, g_JavaMethods[method].m_Method, args);
    if (!CheckJavaException(env, g_JavaMethods[method].m_Name)) return false;
    value = result;
    return true;
}

static bool StartJavaRequest(bool testDevice, const char* testDeviceId, int id, bool deferForm, bool& accepted)
{
    dmAndroid::ThreadAttacher thread;
    JNIEnv* env = thread.GetEnv();
    if (!JavaReady(env, JAVA_REQUEST_CONSENT_INFO_UPDATE)) return false;
    jstring deviceId = env->NewStringUTF(testDeviceId);
    if (!CheckJavaException(env, "test device ID") || !deviceId) return false;
    jvalue args[5] = {};
    args[0].l = dmGraphics::GetNativeAndroidActivity();
    args[1].z = testDevice ? JNI_TRUE : JNI_FALSE;
    args[2].l = deviceId;
    args[3].i = id;
    args[4].z = deferForm ? JNI_TRUE : JNI_FALSE;
    jboolean result = env->CallStaticBooleanMethodA(g_JavaClass,
            g_JavaMethods[JAVA_REQUEST_CONSENT_INFO_UPDATE].m_Method, args);
    env->DeleteLocalRef(deviceId);
    if (!CheckJavaException(env, "requestConsentInfoUpdate")) return false;
    accepted = result == JNI_TRUE;
    return true;
}

static int PushJavaBoolean(lua_State* L, JavaMethod method)
{
    DM_LUA_STACK_CHECK(L, 1);
    bool value;
    if (!CallJavaBoolean(method, 0, value)) return luaL_error(L, "UMP Java call failed: %s", g_JavaMethods[method].m_Name);
    lua_pushboolean(L, value);
    return 1;
}

static int PushJavaInt(lua_State* L, JavaMethod method, const jvalue* args = 0)
{
    DM_LUA_STACK_CHECK(L, 1);
    int value;
    if (!CallJavaInt(method, args, value)) return luaL_error(L, "UMP Java call failed: %s", g_JavaMethods[method].m_Name);
    lua_pushinteger(L, value);
    return 1;
}

static int RequestConsentInfoUpdate(lua_State* L)
{
    DM_LUA_STACK_CHECK(L, 0);
    luaL_checktype(L, 1, LUA_TBOOLEAN);
    const char* testDeviceId = luaL_checkstring(L, 2);
    if (!lua_isnoneornil(L, 3)) luaL_checktype(L, 3, LUA_TFUNCTION);
    if (!lua_isnoneornil(L, 4)) luaL_checktype(L, 4, LUA_TBOOLEAN);
    bool deferForm = lua_toboolean(L, 4);
    int id = ++g_NextId;
    bool accepted;
    if (!StartJavaRequest(lua_toboolean(L, 1), testDeviceId, id, deferForm, accepted)) {
        return luaL_error(L, "UMP Java call failed: requestConsentInfoUpdate");
    }
    if (!accepted) return luaL_error(L, "A UMP form is already loading or showing");
    // A rejected request must leave the active operation and its callback intact.
    ClearCallback(g_RequestCallback);
    ClearCallback(g_InfoCallback);
    g_RequestId = id;
    dmScript::LuaCallbackInfo* callback = lua_isnoneornil(L, 3) ? 0 : dmScript::CreateCallback(L, 3);
    if (deferForm) g_InfoCallback = callback;
    else g_RequestCallback = callback;
    return 0;
}

static int StartJavaForm(lua_State* L, JavaMethod method, int id)
{
    jvalue args[2] = {};
    args[0].l = dmGraphics::GetNativeAndroidActivity();
    args[1].i = id;
    bool accepted;
    if (!CallJavaBoolean(method, args, accepted)) return luaL_error(L, "UMP Java call failed: %s", g_JavaMethods[method].m_Name);
    if (!accepted) return luaL_error(L, "Consent information must finish updating and any active UMP form must close first");
    return 0;
}

static int ShowConsentFormIfRequired(lua_State* L)
{
    DM_LUA_STACK_CHECK(L, 0);
    if (!lua_isnoneornil(L, 1)) luaL_checktype(L, 1, LUA_TFUNCTION);
    if (g_InfoCallback || g_RequestCallback) {
        return luaL_error(L, "Consent information must finish updating before showing a form");
    }
    StartJavaForm(L, JAVA_SHOW_CONSENT_FORM_IF_REQUIRED, g_RequestId);
    g_RequestCallback = lua_isnoneornil(L, 1) ? 0 : dmScript::CreateCallback(L, 1);
    return 0;
}

static int ShowPrivacyOptionsForm(lua_State* L)
{
    DM_LUA_STACK_CHECK(L, 0);
    if (!lua_isnoneornil(L, 1)) luaL_checktype(L, 1, LUA_TFUNCTION);
    int id = ++g_NextId;
    StartJavaForm(L, JAVA_SHOW_PRIVACY_OPTIONS_FORM, id);
    ClearCallback(g_PrivacyCallback);
    g_PrivacyId = id;
    g_PrivacyCallback = lua_isnoneornil(L, 1) ? 0 : dmScript::CreateCallback(L, 1);
    return 0;
}

static int IsPrivacyOptionsRequired(lua_State* L) { return PushJavaBoolean(L, JAVA_IS_PRIVACY_OPTIONS_REQUIRED); }
static int CanRequestAds(lua_State* L) { return PushJavaBoolean(L, JAVA_CAN_REQUEST_ADS); }
static int GetConsentStatus(lua_State* L) { return PushJavaInt(L, JAVA_GET_CONSENT_STATUS); }
static int GetRequestState(lua_State* L) { return PushJavaInt(L, JAVA_GET_REQUEST_STATE); }
static int WasConsentFormRequired(lua_State* L) { return PushJavaBoolean(L, JAVA_WAS_CONSENT_FORM_REQUIRED); }
static int GetPrivacyOptionsState(lua_State* L) { return PushJavaInt(L, JAVA_GET_PRIVACY_OPTIONS_STATE); }

static int ResetConsentInformation(lua_State* L)
{
    DM_LUA_STACK_CHECK(L, 0);
    bool accepted;
    if (!CallJavaBoolean(JAVA_RESET_CONSENT_INFORMATION, 0, accepted)) return luaL_error(L, "UMP Java call failed: resetConsentInformation");
    if (!accepted) return luaL_error(L, "A UMP form must close before resetting consent information");
    ClearCallback(g_RequestCallback);
    ClearCallback(g_InfoCallback);
    ClearCallback(g_PrivacyCallback);
    g_RequestId = 0;
    g_PrivacyId = 0;
    return 0;
}

static int GetGdprApplies(lua_State* L)
{
    jvalue args[1] = {};
    args[0].l = dmGraphics::GetNativeAndroidActivity();
    return PushJavaInt(L, JAVA_GET_GDPR_APPLIES, args);
}

static bool PushPurposeConsents(lua_State* L)
{
    dmAndroid::ThreadAttacher thread;
    JNIEnv* env = thread.GetEnv();
    if (!JavaReady(env, JAVA_GET_PURPOSE_CONSENTS)) return false;
    jstring value = (jstring)env->CallStaticObjectMethod(g_JavaClass,
            g_JavaMethods[JAVA_GET_PURPOSE_CONSENTS].m_Method, dmGraphics::GetNativeAndroidActivity());
    if (!CheckJavaException(env, "getPurposeConsents")) return false;
    if (!value) {
        lua_pushnil(L);
        return true;
    }
    const char* chars = env->GetStringUTFChars(value, 0);
    bool ok = CheckJavaException(env, "purpose consents string") && chars;
    if (ok) {
        lua_pushstring(L, chars);
        env->ReleaseStringUTFChars(value, chars);
    }
    env->DeleteLocalRef(value);
    return ok;
}

static int GetPurposeConsents(lua_State* L)
{
    DM_LUA_STACK_CHECK(L, 1);
    if (!PushPurposeConsents(L)) return luaL_error(L, "UMP Java call failed: getPurposeConsents");
    return 1;
}

static const luaL_reg Module_methods[] = {
    {"request_consent_info_update", RequestConsentInfoUpdate},
    {"show_consent_form_if_required", ShowConsentFormIfRequired},
    {"show_privacy_options_form", ShowPrivacyOptionsForm},
    {"is_privacy_options_required", IsPrivacyOptionsRequired},
    {"can_request_ads", CanRequestAds},
    {"get_consent_status", GetConsentStatus},
    {"get_request_state", GetRequestState},
    {"was_consent_form_required", WasConsentFormRequired},
    {"get_privacy_options_state", GetPrivacyOptionsState},
    {"get_gdpr_applies", GetGdprApplies},
    {"get_purpose_consents", GetPurposeConsents},
    {"reset_consent_information", ResetConsentInformation},
    {0, 0}
};

static void LuaInit(lua_State* L)
{
    int top = lua_gettop(L);
    luaL_register(L, MODULE_NAME, Module_methods);
    #define SETCONSTANT(name) lua_pushinteger(L, name); lua_setfield(L, -2, #name);
    SETCONSTANT(REQUEST_STATE_UPDATING)
    SETCONSTANT(REQUEST_STATE_COMPLETE)
    SETCONSTANT(REQUEST_STATE_FAILED)
    SETCONSTANT(REQUEST_STATE_FORM_PENDING)
    SETCONSTANT(PRIVACY_OPTIONS_STATE_NOT_SHOWN)
    SETCONSTANT(PRIVACY_OPTIONS_STATE_SHOWING)
    SETCONSTANT(PRIVACY_OPTIONS_STATE_DISMISSED)
    SETCONSTANT(PRIVACY_OPTIONS_STATE_ERROR)
    SETCONSTANT(GDPR_APPLIES_UNKNOWN)
    SETCONSTANT(GDPR_APPLIES_NO)
    SETCONSTANT(GDPR_APPLIES_YES)
    SETCONSTANT(ERROR_BRIDGE)
    #undef SETCONSTANT
    lua_pop(L, 1);
    assert(top == lua_gettop(L));
}

static dmExtension::Result AppInitializeExtension(dmExtension::AppParams* params)
{
    return dmExtension::RESULT_OK;
}

static dmExtension::Result InitializeExtension(dmExtension::Params* params)
{
    if (!g_Mutex) g_Mutex = dmMutex::New();
    {
        dmAndroid::ThreadAttacher thread;
        if (!CacheJavaBindings(thread.GetEnv())) return dmExtension::RESULT_INIT_ERROR;
    }
    {
        DM_MUTEX_SCOPED_LOCK(g_Mutex);
        g_AcceptCompletions = true;
    }
    LuaInit(params->m_L);
    dmLogInfo("Registered %s Extension", MODULE_NAME);
    return dmExtension::RESULT_OK;
}

static dmExtension::Result AppFinalizeExtension(dmExtension::AppParams* params)
{
    return dmExtension::RESULT_OK;
}

static dmExtension::Result FinalizeExtension(dmExtension::Params* params)
{
    {
        DM_MUTEX_SCOPED_LOCK(g_Mutex);
        g_AcceptCompletions = false;
        for (uint32_t i = 0; i < g_Completions.Size(); ++i) free(g_Completions[i].m_ErrorMessage);
        g_Completions.SetSize(0);
    }
    ClearCallback(g_RequestCallback);
    ClearCallback(g_InfoCallback);
    ClearCallback(g_PrivacyCallback);
    g_RequestId = 0;
    g_PrivacyId = 0;
    dmAndroid::ThreadAttacher thread;
    ReleaseJavaBindings(thread.GetEnv());
    return dmExtension::RESULT_OK;
}

#else

static dmExtension::Result AppInitializeExtension(dmExtension::AppParams* params)
{
    dmLogWarning("Registered %s (null) Extension", MODULE_NAME);
    return dmExtension::RESULT_OK;
}

static dmExtension::Result InitializeExtension(dmExtension::Params* params) { return dmExtension::RESULT_OK; }
static dmExtension::Result AppFinalizeExtension(dmExtension::AppParams* params) { return dmExtension::RESULT_OK; }
static dmExtension::Result FinalizeExtension(dmExtension::Params* params) { return dmExtension::RESULT_OK; }

#endif

#if defined(DM_PLATFORM_ANDROID)
DM_DECLARE_EXTENSION(EXTENSION_NAME, LIB_NAME, AppInitializeExtension, AppFinalizeExtension, InitializeExtension, UpdateExtension, 0, FinalizeExtension)
#else
DM_DECLARE_EXTENSION(EXTENSION_NAME, LIB_NAME, AppInitializeExtension, AppFinalizeExtension, InitializeExtension, 0, 0, FinalizeExtension)
#endif
