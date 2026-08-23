// Board config — SENSOR NODE "gate", beside the checkpoint.
//
// Tracked in git deliberately: none of this is secret, and it is the only
// record that this board exists and how it is set. secrets.h holds the
// WiFi password and the device account.
//
// Hardware: ESP32-C3 SuperMini, MQ-9 gas + DHT11 temperature/humidity.
//
// Every reading this node sends is prefixed with NODE_ID, so it reports
// gate_gas, gate_temperature, gate_humidity. That is not cosmetic: the
// backend keeps one live row per kind, so two unnamed nodes overwrite
// each other and the console shows whichever spoke last.
//
// It also means this node has its OWN thresholds, configured against
// those prefixed kinds on the Alerts page. A gas limit next to the mixer
// is not the one you want in an open yard.

#define NODE_ID "gate"

// Reporting route. Commented out = WiFi and HTTP straight to the backend,
// which is right for a node in range of the site AP.
//
// Uncomment to report through the gate master over ESP-NOW instead, for
// a node with no network of its own. EITHER/OR, NOT A FALLBACK: joining
// WiFi moves the radio to the router's channel, and ESP-NOW across two
// channels does nothing at all — no error, no packet, just a sensor that
// looks healthy and reports to nobody.
//
// MASTER_MAC and ESPNOW_CHANNEL come from the line the master prints at
// boot: "# ESP-NOW ready, mac 24:6F:28:AA:BB:CC, channel 1"
//
// Live values, read off the master's own boot line on 2026-08-22:
//   # ESP-NOW ready, mac 88:57:21:79:C3:C4, channel 1
#define REPORT_VIA_ESPNOW
#define MASTER_MAC {0x88, 0x57, 0x21, 0x79, 0xC3, 0xC4}
#define ESPNOW_CHANNEL 1
