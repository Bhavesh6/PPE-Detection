// Camera handling and frame streaming to the detection API.

class CameraFeed {
    constructor(videoElement, canvasElement, statusElement) {
        this.video = videoElement;
        this.canvas = canvasElement;
        this.statusElement = statusElement;
        this.streaming = false;
        this.ctx = this.canvas.getContext('2d');
        this.apiBaseUrl = window.API_BASE_URL;
        this.processingFrame = false;
        // Set by the page from the site policy; empty means "not known yet",
        // in which case nothing is filtered out.
        this.requiredPpe = [];
        this.frameInterval = 500; // Send a frame every 500ms
        this.intervalId = null;
        this.overlayCanvas = document.getElementById('overlayCanvas');
        if (this.overlayCanvas) {
            this.overlayCtx = this.overlayCanvas.getContext('2d');
        }
    }

    async start() {
        try {
            this.updateStatus('Requesting camera access...');
            const stream = await navigator.mediaDevices.getUserMedia({
                video: {
                    width: { ideal: 640 },
                    height: { ideal: 480 },
                    facingMode: 'environment' // Use back camera on mobile if available
                },
                audio: false
            });

            this.video.srcObject = stream;
            this.video.play();
            this.streaming = true;
            this.updateStatus('Camera connected');

            // Set canvas size once video dimensions are known
            this.video.addEventListener('loadedmetadata', () => {
                this.canvas.width = this.video.videoWidth;
                this.canvas.height = this.video.videoHeight;
                if (this.overlayCanvas) {
                    this.overlayCanvas.width = this.video.videoWidth;
                    this.overlayCanvas.height = this.video.videoHeight;
                }
            });

            // Start sending frames
            this.startFrameCapture();

            return true;
        } catch (error) {
            this.updateStatus(`Camera error: ${error.message}`);
            console.error('Camera access error:', error);
            return false;
        }
    }

    stop() {
        if (this.streaming) {
            const stream = this.video.srcObject;
            const tracks = stream.getTracks();

            tracks.forEach(track => track.stop());
            this.video.srcObject = null;
            this.streaming = false;

            if (this.intervalId) {
                clearInterval(this.intervalId);
                this.intervalId = null;
            }

            if (this.overlayCtx) {
                this.overlayCtx.clearRect(0, 0, this.overlayCanvas.width, this.overlayCanvas.height);
            }

            this.updateStatus('Camera stopped');
            return true;
        }
        return false;
    }

    startFrameCapture() {
        if (this.intervalId) {
            clearInterval(this.intervalId);
        }

        this.intervalId = setInterval(() => {
            this.captureAndSendFrame();
        }, this.frameInterval);
    }

    captureAndSendFrame() {
        if (!this.streaming || this.processingFrame) return;

        this.processingFrame = true;

        // Ensure canvas has valid dimensions
        if (!this.canvas.width || this.canvas.width === 0) {
            this.canvas.width = 640;
            this.canvas.height = 480;
        }

        // Draw current video frame to canvas
        this.ctx.drawImage(this.video, 0, 0, this.canvas.width, this.canvas.height);

        // Get frame as base64 data URL
        const frameData = this.canvas.toDataURL('image/jpeg', 0.8);

        // Send frame to server
        this.sendFrameToServer(frameData)
            .then(result => {
                if (result && result.processed && result.detections) {
                    this.drawBoundingBoxes(result.detections);
                }
            })
            .catch(error => {
                console.error('Error sending frame:', error);
            })
            .finally(() => {
                this.processingFrame = false;
            });
    }

    /* Detections the current site policy actually cares about.

       The model reports every class it knows, but drawing all of them means
       a site that doesn't require masks still paints a red "NO-Mask" alarm
       over someone's face — accusing them of breaking a rule that doesn't
       exist here. People are shown always; equipment only when required. */
    isRelevant(type) {
        if (type === 'Person') return true;
        const required = this.requiredPpe;
        if (!required || !required.length) return true;   // policy unknown yet
        const item = type.startsWith('NO-') ? type.slice(3) : type;
        return required.includes(item);
    }

    drawBoundingBoxes(detections) {
        if (!this.overlayCtx || !this.overlayCanvas) return;

        // Clear previous drawings
        this.overlayCtx.clearRect(0, 0, this.overlayCanvas.width, this.overlayCanvas.height);

        detections.forEach(det => {
            if (!det.box) return;
            if (!this.isRelevant(det.type)) return;

            const [x1, y1, x2, y2] = det.box;
            const width = x2 - x1;
            const height = y2 - y1;

            // Determine color
            let color = '#FFFF00'; // Yellow
            if (det.type === 'Hardhat' || det.type === 'helmet') color = '#00FF00';
            else if (det.type === 'Safety Vest' || det.type === 'vest') color = '#FFA500';
            else if (det.type === 'Gloves' || det.type === 'hand gloves') color = '#FF00FF';
            else if (det.type.startsWith('NO-')) color = '#FF0000';

            // Draw box
            this.overlayCtx.strokeStyle = color;
            this.overlayCtx.lineWidth = 4;
            this.overlayCtx.strokeRect(x1, y1, width, height);

            // Draw label background
            this.overlayCtx.fillStyle = color;
            const text = `${det.type} ${Math.round(det.confidence * 100)}%`;
            this.overlayCtx.font = '16px Arial';
            const textWidth = this.overlayCtx.measureText(text).width;
            this.overlayCtx.fillRect(x1, y1 - 24, textWidth + 10, 24);

            // Draw text
            this.overlayCtx.fillStyle = '#000000';
            this.overlayCtx.fillText(text, x1 + 5, y1 - 6);
        });
    }

    async sendFrameToServer(frameData) {
        try {
            const response = await Auth.fetch('/api/socket', {
                method: 'POST',
                body: JSON.stringify({ frame: frameData })
            });

            if (!response.ok) {
                throw new Error(`Server responded with ${response.status}`);
            }

            return await response.json();
        } catch (error) {
            console.error('Frame processing error:', error);
            this.updateStatus(`Processing error: ${error.message}`);
            return null;
        }
    }

    updateStatus(message) {
        if (this.statusElement) {
            this.statusElement.textContent = message;
        }
        console.log('Camera status:', message);
    }
}

// Export the class
window.CameraFeed = CameraFeed;
