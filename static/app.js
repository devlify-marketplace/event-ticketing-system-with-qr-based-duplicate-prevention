(() => {
  const form = document.querySelector('#scan-form');
  if (!form) return;
  const input = document.querySelector('#ticket-token');
  const eventSelect = document.querySelector('#event_id');
  const result = document.querySelector('#scan-result');
  const cameraButton = document.querySelector('#camera-toggle');
  const video = document.querySelector('#camera');
  const cameraWrap = document.querySelector('#camera-wrap');
  let stream = null;
  let detector = null;
  let scanning = false;
  let lastCameraValue = '';

  function setResult(data) {
    result.hidden = false;
    result.className = `scan-result ${data.outcome || 'invalid'}`;
    const message = document.createElement('div');
    message.textContent = data.message || 'Unable to validate this ticket.';
    result.replaceChildren(message);
    if (data.attendee || data.event || data.used_at) {
      const details = document.createElement('small');
      details.textContent = [data.attendee, data.event, data.used_at ? `Previously scanned ${data.used_at}` : ''].filter(Boolean).join(' · ');
      result.append(details);
    }
  }

  async function validateTicket(raw) {
    let token = String(raw || '').trim();
    // Most QR scanners return the encoded value; tolerate a URL-wrapped QR too.
    if (/^https?:\/\//i.test(token)) {
      try { token = new URL(token).pathname.split('/').filter(Boolean).pop() || token; } catch (_) { /* leave raw text */ }
    }
    if (!token || !eventSelect?.value) return;
    input.value = token;
    input.disabled = true;
    try {
      const response = await fetch('/api/gate/validate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.content || '' },
        body: JSON.stringify({ ticket: token, event_id: Number(eventSelect.value) })
      });
      const data = await response.json();
      setResult(data);
      if (data.outcome === 'accepted') {
        input.value = '';
        input.focus();
      }
    } catch (_) {
      setResult({ outcome: 'invalid', message: 'Could not reach the check-in service. Check your connection and try again.' });
    } finally {
      input.disabled = false;
    }
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    validateTicket(input.value);
  });

  async function stopCamera() {
    scanning = false;
    if (stream) stream.getTracks().forEach((track) => track.stop());
    stream = null;
    if (video) video.srcObject = null;
    if (cameraWrap) cameraWrap.hidden = true;
    if (cameraButton) cameraButton.textContent = 'Use camera';
  }

  async function scanFrame() {
    if (!scanning || !detector || !video || video.readyState < 2) {
      if (scanning) requestAnimationFrame(scanFrame);
      return;
    }
    try {
      const codes = await detector.detect(video);
      if (codes.length && codes[0].rawValue && codes[0].rawValue !== lastCameraValue) {
        lastCameraValue = codes[0].rawValue;
        await validateTicket(lastCameraValue);
        window.setTimeout(() => { lastCameraValue = ''; }, 1800);
      }
    } catch (_) { /* camera frame may not be ready yet */ }
    if (scanning) window.setTimeout(() => requestAnimationFrame(scanFrame), 120);
  }

  cameraButton?.addEventListener('click', async () => {
    if (scanning) { await stopCamera(); return; }
    if (!('BarcodeDetector' in window) || !navigator.mediaDevices?.getUserMedia) {
      setResult({ outcome: 'invalid', message: 'Camera QR scanning is not supported here. Use a USB/Bluetooth QR reader or paste the ticket code.' });
      return;
    }
    try {
      detector = new BarcodeDetector({ formats: ['qr_code'] });
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' } }, audio: false });
      video.srcObject = stream;
      cameraWrap.hidden = false;
      await video.play();
      scanning = true;
      cameraButton.textContent = 'Stop camera';
      scanFrame();
    } catch (_) {
      await stopCamera();
      setResult({ outcome: 'invalid', message: 'Camera access was unavailable. Allow camera access or use a QR reader/paste a ticket code.' });
    }
  });

  window.addEventListener('pagehide', stopCamera);
})();
