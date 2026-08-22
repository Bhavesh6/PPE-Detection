// Board config — CAMERA "cam", the gate-facing camera.
//
// Tracked in git deliberately: none of this is secret, and it is the only
// record that this board exists and how it is set. secrets.h holds the
// WiFi password and nothing else.
//
// Hardware: ESP32-CAM, OV2640 sensor (hardware JPEG encoder, runs at VGA).
// Reached at: http://safetyfirst-cam.local
//
// The id is "cam" rather than "gate" for historical reasons — it was the
// first camera, named before there was a second. Renaming it means
// reflashing this board AND updating SAFETYFIRST_CCTV_URL on the Pi, so
// it stays as it is until something forces the change.

#define CAM_ID "cam"

// This module is mounted inverted. Flip in firmware, not CSS: /snapshot
// has no CSS, and the Pi's relay would otherwise hand the console an
// upside-down frame.
#define CAM_VFLIP   1
#define CAM_HMIRROR 1

// Tuned for a watchable feed rather than a detailed one. This sensor
// never decides anything - PPE is judged from the gate's own webcam - so
// resolution buys nothing here and costs frames on a slow uplink.
//
// Deliberately at the low end. VGA was measured at ~28KB a frame and
// visibly lagged; QVGA is nearer a quarter of that, and quality 18
// shaves it further at a cost you cannot see at this size on a monitoring
// tile. Start low and raise it if the link turns out to have room:
//
//   http://safetyfirst-cam.local/control?var=framesize&val=8   (VGA)
//   http://safetyfirst-cam.local/control?var=quality&val=12    (sharper)
//
// Those take effect immediately and are not persisted, so a power cycle
// returns the camera to what is set here.
#define CAM_FRAMESIZE FRAMESIZE_QVGA
#define CAM_QUALITY   18
