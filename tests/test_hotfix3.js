/* Hotfix round 3 — urgent text rule, Node suite (no dependencies).
 *
 * WHY THIS EXISTS: round 2 fixed the Urgent card subtext and the
 * "Urgent — handle first" section in static/js/app.js (both now read
 * the server urgent count instead of the Primary-page-1 pool), but the
 * live browser kept showing "Nothing marked urgent" / "Nothing urgent
 * right now." with count 2. Root cause was NOT the text logic — it was
 * stale JS: app/main.py told browsers to cache /js/* for an hour
 * (max-age=3600) and index.html loaded js/app.js with no version
 * query, so the 08:46 live check ran the cached hotfix-1 app.js
 * (verified: fb2e20b6 on GitHub HAS the fix; fc3a8fa7 does NOT, and
 * its subtext was pool-only). Round 3 therefore (a) serves static
 * assets with no-cache + versioned ?v= URLs (see app/main.py) and
 * (b) extracts the urgent text rule into the two pure helpers below,
 * so the rule itself is directly testable here.
 *
 * This file extracts urgentSubtext / urgentSectionHtml from the
 * SHIPPED app.js (brace-matched, not a copy) and asserts their
 * behaviour for count > 0 and count == 0, plus that both call sites
 * actually use them. Run with: node tests/test_hotfix3.js
 * (also run from tests/test_hotfix3.py).
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const APP_JS = path.join(__dirname, "..", "static", "js", "app.js");
const src = fs.readFileSync(APP_JS, "utf8");

function extractFunction(src, name) {
  const start = src.indexOf("function " + name + "(");
  if (start === -1) throw new Error("function " + name + " not found in app.js");
  const brace = src.indexOf("{", start);
  let depth = 0;
  for (let i = brace; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error("unbalanced braces in " + name);
}

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

const ctx = vm.createContext({ escapeHtml });
for (const name of ["urgentSubtext", "urgentSectionHtml"]) {
  vm.runInContext(extractFunction(src, name), ctx);
}
const { urgentSubtext, urgentSectionHtml } = ctx;

let failures = 0;
function check(label, actual, expected) {
  if (actual === expected) {
    console.log("ok   " + label);
  } else {
    failures++;
    console.log("FAIL " + label + "\n  expected: " + JSON.stringify(expected) + "\n  actual:   " + JSON.stringify(actual));
  }
}
function checkContains(label, actual, needle) {
  if (actual.indexOf(needle) !== -1) console.log("ok   " + label);
  else { failures++; console.log("FAIL " + label + "\n  expected to contain: " + JSON.stringify(needle) + "\n  actual: " + JSON.stringify(actual)); }
}
function checkNotContains(label, actual, needle) {
  if (actual.indexOf(needle) === -1) console.log("ok   " + label);
  else { failures++; console.log("FAIL " + label + "\n  expected NOT to contain: " + JSON.stringify(needle) + "\n  actual: " + JSON.stringify(actual)); }
}

/* The live case: server says 2 urgent, pool holds none. */
check("subtext count=2, no pool urgent",
  urgentSubtext(2, null),
  "2 urgent mails in your inbox — open Urgent to see them");
check("section count=2, no pool urgent",
  urgentSectionHtml(2),
  '<div class="empty">2 urgent mails in your inbox — use View all above to open the Urgent filter.</div>');
checkContains("subtext count=2 names the count", urgentSubtext(2, null), "2 urgent mails");
checkNotContains("subtext count=2 never says nothing", urgentSubtext(2, null), "Nothing marked urgent");
checkNotContains("section count=2 never says nothing", urgentSectionHtml(2), "Nothing urgent right now.");

/* Singular. */
check("subtext count=1",
  urgentSubtext(1, null),
  "1 urgent mail in your inbox — open Urgent to see them");
checkContains("section count=1 singular", urgentSectionHtml(1), "1 urgent mail in your inbox");

/* Zero is the only case where "nothing" is correct. */
check("subtext count=0", urgentSubtext(0, null), "Nothing marked urgent");
check("section count=0",
  urgentSectionHtml(0),
  '<div class="empty">Nothing urgent right now.</div>');

/* Pool has an urgent thread: priority subject wins. */
check("subtext pool urgent wins",
  urgentSubtext(2, "Pay the invoice"),
  "Priority: <b>Pay the invoice</b>");

/* Call-site wiring: the two render sites must use the helpers, and the
   old inline pool-only ternaries must not exist anywhere else. */
checkContains("renderStats calls urgentSubtext with the server count",
  src, "urgentSubtext(c.urgent");
checkContains("renderLists calls urgentSectionHtml with the server count",
  src, "urgentSectionHtml(urgentCount)");
const nothingCount = (src.match(/Nothing marked urgent/g) || []).length;
check("exactly one 'Nothing marked urgent' (inside the helper)", nothingCount, 1);
const nothingSection = (src.match(/Nothing urgent right now\./g) || []).length;
check("exactly one 'Nothing urgent right now.' (inside the helper)", nothingSection, 1);

if (failures) {
  console.log("\n" + failures + " failure(s)");
  process.exit(1);
}
console.log("\nall urgent-text checks passed");
