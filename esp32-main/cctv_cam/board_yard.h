// Board config — CAMERA "yard", watching the open area.
//
// Tracked in git deliberately: none of this is secret, and it is the only
// record that this board exists and how it is set. secrets.h holds the
// WiFi password and nothing else.
//
// Hardware: ESP32-CAM, GC2145 sensor (marked RHYX-M21-45 / M12-45).
// Reached at: http://safetyfirst-yard.local
//
// THIS SENSOR CANNOT ENCODE JPEG. It emits RGB565/YUV/RAW only, so the
// firmware compresses every frame on the CPU and drops to QVGA to afford
// it. Nothing here selects that — the sketch tries hardware JPEG, catches
// the refusal at init, and re-initialises. Serial confirms which path it
// took:
//
//     [cam] sensor: GC2145 (RHYX-M21-45) (PID 0x2145)
//     [cam] software (slower) JPEG, PSRAM found
//
// Because it is slower, poll it less often than the OV2640 board. On the
// Pi: SAFETYFIRST_CCTV_INTERVAL_YARD=3.0

#define CAM_ID "yard"

// Verify against the live picture before trusting these — module
// orientation varies even between boards of the same type.
#define CAM_VFLIP   1
#define CAM_HMIRROR 1

// No CAM_FRAMESIZE or CAM_QUALITY here, deliberately. Both belong to the
// hardware-JPEG path, and this sensor never reaches it: the sketch tries
// JPEG, is refused at init, and re-initialises as RGB565 at QVGA with a
// buffer sized to match. Setting them here would read as configuration
// that does something, and it would not.
//
// /control?var=framesize is capped at QVGA on this board for the same
// reason - the buffer was allocated for QVGA, so a larger frame would be
// written past the end of it.
