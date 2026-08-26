from pathlib import Path

ROOT = Path('android-client')

def r(rel, old, new):
    p = ROOT / rel
    s = p.read_text()
    if old not in s:
        raise SystemExit(f'missing patch target in {rel}: {old[:100]!r}')
    p.write_text(s.replace(old, new))

api = 'app/src/main/java/in/vigarepo/ecourtglass/EcourtsApi.java'
bridge = 'app/src/main/java/in/vigarepo/ecourtglass/AppBridge.java'
js = 'app/src/main/assets/app.js'
html = 'app/src/main/assets/index.html'

# Current official eCourts Services 4.0.5 mobile route family.
r(api, 'https://app.ecourts.gov.in/ecourt_mobile_DC/', 'https://app.ecourts.gov.in/services_DC_4.0/')
r(api, 'https://app.ecourts.gov.in/ecourt_mobile_HC/', 'https://app.ecourts.gov.in/services_HC_4.0/')
r(api, '// This value belongs to the verified ecourt_mobile_* protocol generation.\n    private static final String APP_PROTOCOL_VERSION = "3.0";', '// Current mobile API generation recovered from the official eCourts Services 4.0.5 APK.\n    private static final String APP_PROTOCOL_VERSION = "4.0";')
r(api, 'o.put("protocolFamily", "ecourt_mobile");', 'o.put("protocolFamily", "services_4.0");')
r(api, 'con.setRequestProperty("User-Agent", "eCourtGlass/2 Android");', 'con.setRequestProperty("User-Agent", "eCourtGlass/3 Android");')

# Current endpoint names recovered from 4.0.5; keep old aliases available for compatibility.
r(api, '"courtNameWebService.php", "policeStationWebService.php", "actWebService.php", "caseNumberWebService.php",', '"courtNameWebService.php", "policeStationWebService.php", "actWebService.php", "caseTypesWebService.php", "caseNumberWebService.php",')
r(api, '"firNumberSearch.php", "pretrialNumberSearch.php", "showDataWebService.php", "searchCaveat.php",', '"firNumberSearch.php", "pretrialNumberSearch.php", "searchByPartyName.php", "showDataWebService.php", "searchCaveat.php",')
r(api, '"lowerCourtCaveat.php", "caveatCount.php", "cases_new.php", "latlong.php"', '"lowerCourtCaveat.php", "caveatCount.php", "cases_new.php", "latlong.php",\n            "display_pdf_new.php", "preTrialOrder_pdf.php", "ia_show_business.php"')

# Use no Authorization header when no session token exists; uid retry can mint/refresh a token.
r(api, 'response = roundTrip(endpoint, retry, courtFamily, token == null ? "" : token);', 'response = roundTrip(endpoint, retry, courtFamily, (token == null || token.isEmpty()) ? null : token);')

# Do not make a missing bootstrap token fatal: current RN builds can establish auth on the operation.
old = '''    private void ensureSession(String family) throws Exception {\n        if (token != null && !token.isEmpty()) return;\n        JSONObject p = new JSONObject();\n        p.put("version", APP_PROTOCOL_VERSION);\n        p.put("uid", uid());\n        throttle();\n        JSONObject response = roundTrip("appReleaseWebService.php", p, family, null);\n        captureToken(response);\n        if (token == null || token.isEmpty()) {\n            throw new IllegalStateException(serverMessage(response, "Session bootstrap returned no token"));\n        }\n    }\n'''
new = '''    private boolean bootstrapAttempted = false;\n\n    private void ensureSession(String family) throws Exception {\n        if ((token != null && !token.isEmpty()) || bootstrapAttempted) return;\n        bootstrapAttempted = true;\n        JSONObject p = new JSONObject();\n        p.put("version", APP_PROTOCOL_VERSION);\n        p.put("uid", uid());\n        try {\n            throttle();\n            JSONObject response = roundTrip("appReleaseWebService.php", p, family, null);\n            captureToken(response);\n        } catch (Exception ignored) {\n            // The operation itself is authoritative. If this generation establishes its token on\n            // the uid-assisted operation, request() below will handle that normal 401 flow.\n        }\n    }\n'''
r(api, old, new)
r(api, '        token = null;\n        prefs.edit().remove("session_token").apply();', '        token = null;\n        bootstrapAttempted = false;\n        prefs.edit().remove("session_token").apply();')

# Remove obsolete v2 custom bases and stale session tokens on first v3 launch.
old = '''    private void migrateLegacyBases() {\n        String dc = prefs.getString("dc_base", "");\n        String hc = prefs.getString("hc_base", "");\n        SharedPreferences.Editor edit = prefs.edit();\n        boolean changed = false;\n        if (dc.contains("/services_DC_4.0")) { edit.remove("dc_base"); changed = true; }\n        if (hc.contains("/services_HC_4.0")) { edit.remove("hc_base"); changed = true; }\n        if (changed) edit.remove("session_token").apply();\n    }\n'''
new = '''    private void migrateLegacyBases() {\n        String dc = prefs.getString("dc_base", "");\n        String hc = prefs.getString("hc_base", "");\n        String marker = prefs.getString("protocol_marker", "");\n        SharedPreferences.Editor edit = prefs.edit();\n        boolean changed = false;\n        if (dc.contains("/ecourt_mobile_DC") || dc.contains("/ecourt_mobile_encrypted_DC")) { edit.remove("dc_base"); changed = true; }\n        if (hc.contains("/ecourt_mobile_HC") || hc.contains("/ecourt_mobile_encrypted_HC")) { edit.remove("hc_base"); changed = true; }\n        if (!"services_4.0_v3".equals(marker)) { edit.putString("protocol_marker", "services_4.0_v3"); changed = true; }\n        if (changed) edit.remove("session_token").apply();\n    }\n'''
r(api, old, new)

# Actionable errors instead of generic failures.
r(api, '        if (code == 405) {\n            throw new IllegalStateException("HTTP 405 from eCourts. Reset API bases in Settings; this build uses the verified ecourt_mobile GET protocol.");\n        }', '        if (code == 404) {\n            throw new IllegalStateException("HTTP 404 from eCourts for " + endpoint + " at " + base(family) + ". Restore official API defaults in Tools if a custom base was saved.");\n        }\n        if (code == 405) {\n            throw new IllegalStateException("HTTP 405 from eCourts on the Services 4.0 mobile route. Restore official API defaults in Tools if a custom base was saved.");\n        }')

# UI routes and protocol values.
r(js, "party:{label:'Party name',ep:'showDataWebService.php'", "party:{label:'Party name',ep:'searchByPartyName.php'")
r(js, "payload.version_number='3.0';", "payload.version_number='4.0';")
r(js, "ep:'caseNumberWebService.php'", "ep:'caseTypesWebService.php'")
r(js, "function runLookup(i){var x=LOOKUPS[i],p=x.build();p.language_flag=p.language_flag||'english';p.bilingual_flag=p.bilingual_flag||'0';$('#lookupResult').innerHTML='<div class=\"status-line\">Loading '+safe(x.name)+'…</div>';nativeRequest(x.ep,p,activeFamily)", "function runLookup(i){var x=LOOKUPS[i],p=x.build(),ep=(i===0&&activeFamily==='HC')?'stateWebService_hc.php':x.ep;p.language_flag=p.language_flag||'english';p.bilingual_flag=p.bilingual_flag||'0';$('#lookupResult').innerHTML='<div class=\"status-line\">Loading '+safe(x.name)+'…</div>';nativeRequest(ep,p,activeFamily)")

# Connection button now performs a real API request rather than only re-rendering local state.
r(html, '<button class="secondary" onclick="refreshSessionCard()">Check</button>', '<button class="secondary" onclick="testConnection()">Test connection</button>')
needle = "function clearSession(){Native.clearSession();refreshSessionCard();toast('Session cleared; next request will create a new one');}window.clearSession=clearSession;"
test = "function testConnection(){var badge=$('#sessionBadge');badge.className='badge neutral';badge.textContent='Testing…';var ep=activeFamily==='HC'?'stateWebService_hc.php':'stateWebService.php';nativeRequest(ep,{action_code:'fillState',time:String(Date.now()/1000),language_flag:'english',bilingual_flag:'0'},activeFamily).then(function(){badge.className='badge good';badge.textContent='Connected';refreshSessionCard();toast('eCourts connection working');},function(e){badge.className='badge bad';badge.textContent='Connection error';toast(e.message);});}window.testConnection=testConnection;\n" + needle
r(js, needle, test)

# Sync and app metadata.
r(bridge, 'payload.put("version_number", "3.0");', 'payload.put("version_number", "4.0");')
r(bridge, 'o.put("version", "2.0.0");', 'o.put("version", "3.0.0");')
r(bridge, 'o.put("protocol", "eCourts ecourt_mobile");', 'o.put("protocol", "eCourts Services 4.0");')
r('app/build.gradle.kts', 'versionCode = 2', 'versionCode = 3')
r('app/build.gradle.kts', 'versionName = "2.0.0"', 'versionName = "3.0.0"')

print('Applied eCourt Glass v3 / official Services 4.0.5 compatibility patch')
