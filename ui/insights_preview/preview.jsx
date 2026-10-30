/**
 * Standalone preview of misight's Data Insights page, with no Okta login.
 *
 * Renders the real page component (misight/src/screens/discovery/insights) with misight's real
 * store and theme, against the local agent on :8080. Instead of an Okta token it sends an
 * unsigned MOCK token whose `mock_role` claim the sample MCP server returns as the user's role,
 * so each role gets its own persona. All data is made up (mcp_server/marketpro_sample_data.json).
 * The "Viewing as" pill (top right) switches the mock role.
 */
import React, { useMemo, useState } from "react";
import { AppRegistry } from "react-native";

import "misight/src/themes/fonts.css";
import { Pressable, Text, View } from "misight/src/components/common";
import DataInsights from "misight/src/screens/discovery/insights";
import { StoreProvider } from "misight/src/state";
import { askInsights, introInsights } from "misight/src/state/insights";
import ThemeWrapper from "misight/src/themes/themeWrapper";

const AGENT_URL = "http://127.0.0.1:8080/invocations";

// misight's Heap analytics script isn't loaded here; log events to the console instead.
if (typeof window.heap?.track !== "function") {
  window.heap = {
    track: (name, props) => console.info("[heap]", name, props),
    identify: () => {},
    addUserProperties: () => {},
    addEventProperties: () => {},
    resetIdentity: () => {},
  };
}

// MarketPro role (users.okta_role) -> the persona the agent should pick.
const ROLES = [
  { role: "Wholesaler", note: "Wholesaler persona" },
  { role: "Divisional Sales Manager", note: "Sales manager persona" },
  { role: "Sales Officer", note: "Sales manager persona" },
  { role: "Key Accounts Manager", note: "Key accounts persona" },
  { role: "Recruiter", note: "Recruiter persona" },
  { role: "Discovery User", note: "default Market research persona" },
];

const b64url = (value) =>
  btoa(JSON.stringify(value)).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");

const mockToken = (role) =>
  `${b64url({ alg: "none", typ: "JWT" })}.${b64url({
    sub: "mock.user@example.com",
    uid: "mock-user",
    cid: "insights-preview",
    group: ["mock"],
    mock_role: role,
  })}.mock`;

const pill = {
  position: "absolute",
  top: 14,
  right: 18,
  zIndex: 10,
  alignItems: "flex-end",
};

function RolePicker({ role, onChange }) {
  const [open, setOpen] = useState(false);
  return (
    <View style={pill}>
      <Pressable
        onPress={() => setOpen(!open)}
        style={{
          backgroundColor: "rgba(20,24,60,0.72)",
          borderRadius: 999,
          paddingVertical: 6,
          paddingHorizontal: 12,
        }}
      >
        <Text style={{ color: "#fff", fontSize: 12 }}>Viewing as {role} ▾</Text>
      </Pressable>
      {open ? (
        <View
          style={{
            marginTop: 6,
            backgroundColor: "#fff",
            borderRadius: 10,
            paddingVertical: 6,
            boxShadow: "0 6px 24px rgba(0,0,0,0.18)",
          }}
        >
          {ROLES.map((r) => (
            <Pressable
              key={r.role}
              onPress={() => {
                onChange(r.role);
                setOpen(false);
              }}
              style={{ paddingVertical: 8, paddingHorizontal: 14 }}
            >
              <Text style={{ fontSize: 13, fontWeight: r.role === role ? "700" : "400" }}>
                {r.role}
              </Text>
              <Text style={{ fontSize: 11, color: "#777" }}>{r.note}</Text>
            </Pressable>
          ))}
        </View>
      ) : null}
    </View>
  );
}

function Preview() {
  const [role, setRole] = useState(ROLES[0].role);
  const { ask, intro } = useMemo(() => {
    const token = mockToken(role);
    return {
      ask: (params) => askInsights({ ...params, url: AGENT_URL, accessToken: token }),
      intro: (params) => introInsights({ ...params, url: AGENT_URL, accessToken: token }),
    };
  }, [role]);

  return (
    <View style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
      <RolePicker role={role} onChange={setRole} />
      {/* A new key per role starts a fresh conversation and agent session. */}
      <DataInsights key={role} ask={ask} intro={intro} />
    </View>
  );
}

function App() {
  return (
    <StoreProvider>
      <ThemeWrapper>
        <Preview />
      </ThemeWrapper>
    </StoreProvider>
  );
}

AppRegistry.registerComponent("insightsPreview", () => App);
AppRegistry.runApplication("insightsPreview", { rootTag: document.getElementById("root") });
