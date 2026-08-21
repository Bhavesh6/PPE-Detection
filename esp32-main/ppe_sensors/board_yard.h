// Board config — SENSOR NODE "yard", the second sensor position.
//
// Tracked in git deliberately: none of this is secret, and it is the only
// record that this board exists and how it is set. secrets.h holds the
// WiFi password and the device account.
//
// Hardware: ESP32-C3 SuperMini, MQ-9 gas + DHT11 temperature/humidity —
// identical to the gate node. Only this file differs between them, which
// is the point: one sketch, two boards, no copy to keep in step.
//
// Reports yard_gas, yard_temperature, yard_humidity. Set thresholds for
// those kinds separately on the Alerts page — an open yard disperses gas
// far faster than an enclosed gate area, so the same number does not mean
// the same thing in both places.

#define NODE_ID "yard"

// Reporting route — see board_gate.h for the full explanation. If this
// node is the one out of AP range, this is the file to uncomment in.
//
// #define REPORT_VIA_ESPNOW
// #define MASTER_MAC {0x24, 0x6F, 0x28, 0xAA, 0xBB, 0xCC}
// #define ESPNOW_CHANNEL 1
