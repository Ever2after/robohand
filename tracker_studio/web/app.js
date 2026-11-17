import * as THREE from 'three';
import { OrbitControls } from './vendor/three/OrbitControls.js';

const element = (id) => document.getElementById(id);
const colors = ['#65dbca', '#e9b66e', '#b7a0ed', '#e292b2'];
const labels = {
  tracking: '추적 중', waiting: '연결 대기', calibrating: '보정 대기',
  inertial: '광학 신호 대기', stale: '수신 끊김',
};
const objects = new Map();
const histories = new Map();
let state = null;
let selectedKey = null;
let captureId = null;
let fileSignature = '';
let deviceSignature = '';
let toastTimer;
let operationPending = false;
let renderer;
let scene;
let camera;
let controls;
let autoFrame = true;

function toast(message, error = false) {
  const target = element('toast');
  target.textContent = message;
  target.classList.toggle('error', error);
  target.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { target.hidden = true; }, error ? 9000 : 6500);
}

async function command(path, body = {}) {
  operationPending = true;
  try {
    const response = await fetch(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '작업을 완료하지 못했습니다.');
    return true;
  } catch (error) {
    toast(error.message, true);
    return false;
  } finally {
    operationPending = false;
  }
}

function formatNumber(value, digits = 3) {
  if (!Number.isFinite(value)) return '—';
  return (Math.abs(value) < 0.5 * 10 ** -digits ? 0 : value).toFixed(digits);
}

function duration(value) {
  const minutes = Math.floor(value / 60).toString().padStart(2, '0');
  return `${minutes}:${(value % 60).toFixed(1).padStart(4, '0')}`;
}

function trackerColor(index) {
  return colors[index % colors.length];
}

function initializeScene() {
  const host = element('viewport');
  THREE.Object3D.DEFAULT_UP.set(0, 0, 1);
  scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2('#141b24', 0.055);
  camera = new THREE.PerspectiveCamera(42, 1, 0.01, 150);
  camera.up.set(0, 0, 1);
  camera.position.set(3.8, -5.2, 3.6);
  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setClearColor(0x12171f, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  host.prepend(renderer.domElement);
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.target.set(0, 0, 0.4);
  controls.minDistance = 0.2;
  controls.maxDistance = 60;
  controls.addEventListener('start', () => { autoFrame = false; });

  scene.add(new THREE.HemisphereLight(0xd3e5ff, 0x283238, 2.8));
  const keyLight = new THREE.DirectionalLight(0xffffff, 3.5);
  keyLight.position.set(2, -2, 5);
  scene.add(keyLight);
  const grid = new THREE.GridHelper(20, 40, 0x435568, 0x263545);
  grid.rotation.x = Math.PI / 2;
  grid.material.transparent = true;
  grid.material.opacity = 0.58;
  scene.add(grid);
  const worldAxes = new THREE.AxesHelper(0.6);
  worldAxes.material.transparent = true;
  worldAxes.material.opacity = 0.7;
  scene.add(worldAxes);
  const origin = new THREE.Mesh(
    new THREE.RingGeometry(0.045, 0.052, 40),
    new THREE.MeshBasicMaterial({ color: 0x7590a6, side: THREE.DoubleSide }),
  );
  origin.position.z = 0.002;
  scene.add(origin);
  new ResizeObserver(() => {
    renderer.setSize(host.clientWidth, host.clientHeight, false);
    camera.aspect = host.clientWidth / host.clientHeight;
    camera.updateProjectionMatrix();
  }).observe(host);
}

function makeDevice(device, color) {
  const group = new THREE.Group();
  const body = new THREE.Group();
  const material = new THREE.MeshStandardMaterial({ color: 0x37434e, roughness: 0.52, metalness: 0.35 });
  const accent = new THREE.MeshStandardMaterial({ color, emissive: color, emissiveIntensity: 0.35 });
  if (device.kind === 'base') {
    body.add(new THREE.Mesh(new THREE.BoxGeometry(0.11, 0.1, 0.12), material));
    const face = new THREE.Mesh(new THREE.BoxGeometry(0.095, 0.006, 0.09), accent);
    face.position.y = -0.053;
    body.add(face);
  } else {
    const center = new THREE.Mesh(new THREE.CylinderGeometry(0.035, 0.043, 0.028, 32), material);
    center.rotation.x = Math.PI / 2;
    body.add(center);
    for (let index = 0; index < 3; index++) {
      const angle = index * Math.PI * 2 / 3;
      const pod = new THREE.Mesh(new THREE.SphereGeometry(0.025, 16, 12), material);
      pod.scale.set(1.05, 1.05, 0.72);
      pod.position.set(Math.cos(angle) * 0.045, Math.sin(angle) * 0.045, 0.008);
      body.add(pod);
    }
    const led = new THREE.Mesh(new THREE.SphereGeometry(0.006, 12, 8), accent);
    led.position.set(0, 0, 0.022);
    body.add(led);
  }
  group.add(body);
  const axes = new THREE.AxesHelper(device.kind === 'base' ? 0.16 : 0.23);
  group.add(axes);
  const labelCanvas = document.createElement('canvas');
  labelCanvas.width = 512;
  labelCanvas.height = 80;
  const context = labelCanvas.getContext('2d');
  context.font = '500 30px Inter, sans-serif';
  context.textAlign = 'center';
  context.fillStyle = color;
  context.fillText(device.key, 256, 45);
  const label = new THREE.Sprite(new THREE.SpriteMaterial({
    map: new THREE.CanvasTexture(labelCanvas), depthTest: false, transparent: true,
  }));
  label.scale.set(0.55, 0.086, 1);
  label.position.z = 0.18;
  group.add(label);
  scene.add(group);
  const trail = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints([]),
    new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.6 }),
  );
  trail.visible = false;
  scene.add(trail);
  return {
    group, axes, body, label, trail, points: [], targetPosition: new THREE.Vector3(),
    targetQuaternion: new THREE.Quaternion(), initialized: false,
  };
}

function clearObjects() {
  for (const visual of objects.values()) {
    scene?.remove(visual.group, visual.trail);
    const materials = new Set();
    visual.group.traverse((child) => {
      child.geometry?.dispose();
      if (child.material) materials.add(child.material);
    });
    for (const material of materials) {
      material.map?.dispose();
      material.dispose();
    }
    visual.trail.geometry.dispose();
    visual.trail.material.dispose();
  }
  objects.clear();
  histories.clear();
  autoFrame = true;
}

function frameAll(view = 'perspective') {
  if (!controls) return;
  const bounds = new THREE.Box3();
  let count = 0;
  for (const visual of objects.values()) {
    if (visual.group.visible) {
      bounds.expandByPoint(visual.targetPosition);
      count++;
    }
  }
  const center = count ? bounds.getCenter(new THREE.Vector3()) : new THREE.Vector3(0, 0, 0.4);
  const radius = count ? Math.max(1.4, bounds.getSize(new THREE.Vector3()).length() * 0.9) : 3;
  const offsets = { perspective: [1.3, -1.7, 1.35], top: [0, -0.001, 2.4], front: [0, -2.5, 0.65] };
  controls.target.copy(center);
  camera.position.copy(center).add(new THREE.Vector3(...offsets[view]).multiplyScalar(radius));
  controls.update();
}

function updateDevices(devices) {
  const trackers = devices.filter((device) => device.kind === 'tracker');
  if (!selectedKey || !trackers.some((device) => device.key === selectedKey)) selectedKey = trackers[0]?.key;
  const signature = JSON.stringify(trackers.map((device) => [device.key, device.serial, device.tracking, device.key === selectedKey]));
  if (signature !== deviceSignature) {
    deviceSignature = signature;
    const list = element('device-list');
    list.replaceChildren();
    trackers.forEach((device, index) => {
      const button = document.createElement('button');
      button.className = `device-card${device.key === selectedKey ? ' selected' : ''}`;
      button.style.setProperty('--device-color', trackerColor(index));
      const top = document.createElement('span');
      top.className = 'device-card-top';
      const swatch = document.createElement('span');
      swatch.className = 'device-color';
      top.append(swatch, document.createTextNode(`트래커 ${index + 1}`));
      const serial = document.createElement('small');
      serial.textContent = device.serial && !/^0+$/.test(device.serial) ? device.serial : `${device.key} · 페어링 대기`;
      const status = document.createElement('span');
      status.className = 'device-card-state';
      const dot = document.createElement('span');
      dot.className = `dot${device.tracking === 'tracking' ? '' : device.tracking === 'stale' ? ' gray' : ' amber'}`;
      status.append(dot, document.createTextNode(labels[device.tracking]));
      button.append(top, serial, status);
      button.onclick = () => { selectedKey = device.key; updateDevices(state.devices); };
      list.append(button);
    });
    if (!trackers.length) list.innerHTML = '<p class="muted empty-small">장치 연결을 기다리고 있습니다.</p>';
  }
  element('tracker-count').textContent = trackers.length;
  let visible = 0;
  for (const device of devices) {
    if (!device.position || device.tracking === 'calibrating') continue;
    visible++;
    if (renderer) {
      let visual = objects.get(device.key);
      if (!visual) {
        const color = device.kind === 'base' ? '#8b9db3' : trackerColor(trackers.findIndex((item) => item.key === device.key));
        visual = makeDevice(device, color);
        objects.set(device.key, visual);
      }
      visual.targetPosition.fromArray(device.position);
      const [w, x, y, z] = device.quaternion;
      visual.targetQuaternion.set(x, y, z, w).normalize();
      if (!visual.initialized) {
        visual.group.position.copy(visual.targetPosition);
        visual.group.quaternion.copy(visual.targetQuaternion);
        visual.initialized = true;
      }
      visual.axes.visible = element('axes-toggle').checked;
      visual.trail.visible = element('trail-toggle').checked && visual.points.length > 1;
      visual.body.scale.setScalar(device.key === selectedKey ? 1.3 : 1);
      visual.label.material.opacity = device.tracking === 'stale' ? 0.35 : 1;
      if (device.kind === 'tracker' && device.tracking === 'tracking') {
        const lastPoint = visual.points.at(-1);
        if (!lastPoint || lastPoint.distanceToSquared(visual.targetPosition) > 0.000025) {
          visual.points.push(visual.targetPosition.clone());
          if (visual.points.length > 1200) visual.points.shift();
          visual.trail.geometry.dispose();
          visual.trail.geometry = new THREE.BufferGeometry().setFromPoints(visual.points);
        }
      }
    }
    if (device.kind === 'tracker') {
      const history = histories.get(device.key) || [];
      if (history.at(-1)?.nativeTime !== device.pose_time_s) {
        history.push({ time: performance.now() / 1000, position: device.position, nativeTime: device.pose_time_s });
      }
      while (history.length && performance.now() / 1000 - history[0].time > 12) history.shift();
      histories.set(device.key, history);
    }
  }
  if (visible && autoFrame) { frameAll(); autoFrame = false; }
  element('viewport-empty').hidden = visible > 0;
  const hint = state?.pairing
    ? '트래커 버튼을 약 2초 눌러 파란색이 깜빡이게 한 뒤 초록색 전환을 확인하세요.'
    : '트래커를 베이스스테이션이 보이는 곳에 고정해 보정을 기다려 주세요.';
  element('waiting-hint').textContent = state?.error || hint;
}

function updateInspector(devices) {
  const trackers = devices.filter((device) => device.kind === 'tracker');
  const device = trackers.find((item) => item.key === selectedKey);
  const index = trackers.findIndex((item) => item.key === selectedKey);
  element('selected-name').textContent = device ? `트래커 ${index + 1} · ${device.key}` : '트래커 선택';
  element('chart-device').textContent = device ? device.key : '선택된 트래커';
  element('selected-serial').textContent = device?.serial && !/^0+$/.test(device.serial) ? device.serial : '페어링 대기';
  element('selected-status').textContent = labels[device?.tracking] || '대기';
  element('selected-status').className = `small-status ${device?.tracking || ''}`;
  ['x', 'y', 'z'].forEach((axis, dimension) => {
    element(`position-${axis}`).textContent = formatNumber(device?.position?.[dimension]);
    element(`rotation-${axis}`).textContent = device?.euler ? `${formatNumber(device.euler[dimension], 2)}°` : '—';
  });
  element('quaternion-values').textContent = device?.quaternion?.map((value) => formatNumber(value, 5)).join('  ') || '—';
  element('sample-rate').innerHTML = `${device ? formatNumber(device.rate_hz, 0) : '—'} <small>Hz</small>`;
  element('sample-age').innerHTML = `${device?.age_s != null ? Math.round(device.age_s * 1000).toLocaleString() : '—'} <small>ms</small>`;
}

function updateFiles(files) {
  const signature = JSON.stringify(files.map((file) => file.name));
  if (signature === fileSignature) return;
  fileSignature = signature;
  element('file-count').textContent = files.length;
  const list = element('file-list');
  list.replaceChildren();
  for (const file of files) {
    const row = document.createElement('div');
    row.className = 'file-item';
    const info = document.createElement('div');
    info.className = 'file-item-info';
    const title = document.createElement('strong');
    title.textContent = file.name;
    title.title = file.name;
    const subtitle = document.createElement('small');
    subtitle.textContent = 'CSV · 원본 좌표 / 회전 / 시간';
    info.append(title, subtitle);
    const download = document.createElement('a');
    download.href = `/api/recordings/${encodeURIComponent(file.name)}`;
    download.className = 'download-link';
    download.title = `${file.name} 다운로드`;
    download.textContent = '↓';
    download.setAttribute('aria-label', `${file.name} 다운로드`);
    row.append(info, download);
    list.append(row);
  }
  if (!files.length) list.innerHTML = '<p class="muted empty-small">기록한 CSV가 여기에 표시됩니다.</p>';
}

function renderState(next) {
  state = next;
  if (captureId !== state.capture_id) { clearObjects(); captureId = state.capture_id; }
  updateDevices(state.devices);
  updateInspector(state.devices);
  const active = state.devices.filter((device) => device.kind === 'tracker' && device.tracking === 'tracking').length;
  const connected = state.status === 'connected';
  const statusText = active ? `${active}개 트래커 추적 중` : connected ? '트래커 대기' : state.status === 'error' ? '연결 오류' : '연결 중';
  element('connection-status').replaceChildren();
  const statusDot = document.createElement('span');
  statusDot.className = `dot${active ? '' : state.status === 'error' ? ' red' : ' amber'}`;
  element('connection-status').append(statusDot, document.createTextNode(statusText));
  element('engine-text').textContent = connected ? 'libsurvive 실행 중' : state.status === 'error' ? '엔진 오류' : '엔진 시작 중';
  element('engine-dot').className = `dot${connected ? '' : ' amber'}`;
  element('receiver-count').textContent = state.receivers.length;
  const receiverSignature = state.receivers.map((receiver) => receiver.serial).join('|');
  if (element('receiver-list').dataset.signature !== receiverSignature) {
    const list = element('receiver-list');
    list.dataset.signature = receiverSignature;
    list.replaceChildren();
    state.receivers.forEach((receiver) => {
      const item = document.createElement('div');
      item.className = 'receiver-item';
      const dot = document.createElement('span');
      dot.className = 'dot';
      item.append(dot, document.createTextNode(receiver.serial));
      list.append(item);
    });
  }
  const recording = state.recording;
  element('record-button').disabled = operationPending || (!recording && !active);
  element('record-button').classList.toggle('recording', Boolean(recording));
  element('record-button-label').textContent = recording ? '기록 중지' : '기록 시작';
  element('record-badge').textContent = recording ? '● RECORDING' : 'CSV';
  element('record-badge').style.color = recording ? '#e9968c' : '';
  const stats = recording || state.last_recording;
  element('record-duration').textContent = duration(stats?.duration_s || 0);
  element('record-samples').textContent = (stats?.samples || 0).toLocaleString();
  element('session-name').disabled = Boolean(recording);
  element('record-hint').textContent = recording ? '수신된 원본 샘플을 로컬 파일에 기록 중입니다.' : active ? '위치 · 회전각 · 쿼터니언 · 타임스탬프 저장' : '광학 추적이 활성화되면 기록할 수 있습니다.';
  for (const id of ['pair-button', 'calibrate-button', 'reconnect-button']) element(id).disabled = Boolean(recording) || operationPending;
  updateFiles(state.files);
  element('save-directory').title = state.recordings_path;
  const bases = state.devices.filter((device) => device.kind === 'base' && device.position).length;
  element('base-status').textContent = `베이스스테이션 ${bases}개 보정 · USB 수신기 ${state.receivers.length}개`;
  const logSignature = JSON.stringify(state.logs);
  if (element('log-list').dataset.signature !== logSignature) {
    const list = element('log-list');
    list.dataset.signature = logSignature;
    list.replaceChildren();
    state.logs.forEach((log) => {
      const line = document.createElement('div');
      line.className = `log-${log.level}`;
      const timestamp = document.createElement('span');
      timestamp.className = 'log-time';
      timestamp.textContent = log.time;
      line.append(timestamp, document.createTextNode(log.message));
      list.append(line);
    });
    list.scrollTop = list.scrollHeight;
  }
}

function drawChart() {
  const canvas = element('telemetry-chart');
  const ratio = Math.min(devicePixelRatio, 2);
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }
  const context = canvas.getContext('2d');
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  const history = histories.get(selectedKey) || [];
  const now = performance.now() / 1000;
  const points = history.filter((point) => point.time > now - 10);
  const values = points.flatMap((point) => point.position);
  let minimum = values.length ? Math.min(...values) : -1;
  let maximum = values.length ? Math.max(...values) : 1;
  const padding = Math.max(0.1, (maximum - minimum) * 0.15);
  minimum -= padding;
  maximum += padding;
  const left = 42;
  const right = width - 7;
  const bottom = height - 18;
  context.font = '9px Inter, sans-serif';
  context.fillStyle = '#53667d';
  context.strokeStyle = '#26323f';
  context.lineWidth = 1;
  for (let index = 0; index <= 2; index++) {
    const y = 8 + (bottom - 8) * index / 2;
    context.beginPath();
    context.moveTo(left, y);
    context.lineTo(right, y);
    context.stroke();
    context.fillText((maximum - (maximum - minimum) * index / 2).toFixed(2), 0, y + 3);
  }
  context.fillText('−10 s', left, height - 2);
  context.fillText('현재', right - 21, height - 2);
  ['#ee8b8b', '#a9d58d', '#83b5f7'].forEach((color, dimension) => {
    context.strokeStyle = color;
    context.lineWidth = 1.5;
    context.beginPath();
    let lastTime = null;
    for (const point of points) {
      const x = left + ((point.time - now + 10) / 10) * (right - left);
      const y = 8 + ((maximum - point.position[dimension]) / (maximum - minimum)) * (bottom - 8);
      if (lastTime === null || point.time - lastTime > 0.5) context.moveTo(x, y);
      else context.lineTo(x, y);
      lastTime = point.time;
    }
    context.stroke();
  });
}

element('reconnect-button').onclick = () => command('/api/connect');
element('pair-button').onclick = async () => {
  if (await command('/api/connect', { pair: true })) toast('페어링 대기 중입니다. 트래커 버튼을 약 2초 눌러 파란색이 깜빡이게 해 주세요.');
};
element('calibrate-button').onclick = async () => {
  if (await command('/api/connect', { calibrate: true })) toast('새 보정을 시작했습니다. 트래커 센서를 가리지 않은 채 여러 위치에서 잠시 고정해 주세요.');
};
element('record-button').onclick = async () => {
  const stopping = Boolean(state?.recording);
  if (await command(stopping ? '/api/record/stop' : '/api/record/start', { label: element('session-name').value })) {
    toast(stopping ? '기록을 저장했습니다. 저장된 기록에서 CSV를 다운로드할 수 있습니다.' : '데이터 기록을 시작했습니다.');
  }
};
element('fit-button').onclick = () => frameAll();
document.querySelectorAll('[data-view]').forEach((button) => {
  button.onclick = () => {
    document.querySelectorAll('[data-view]').forEach((other) => other.classList.toggle('active', other === button));
    frameAll(button.dataset.view);
  };
});
element('clear-trails').onclick = () => {
  for (const visual of objects.values()) {
    visual.points = [];
    visual.trail.geometry.dispose();
    visual.trail.geometry = new THREE.BufferGeometry().setFromPoints([]);
  }
};

try { initializeScene(); }
catch (error) {
  element('render-error').hidden = false;
  element('render-error').textContent = `3D 화면 초기화 오류 — ${error.message}. 브라우저의 WebGL 지원을 확인하세요.`;
}

let lastFrame = performance.now();
let lastChart = 0;
function animate(now) {
  requestAnimationFrame(animate);
  const blend = 1 - Math.exp(-Math.min((now - lastFrame) / 1000, 0.1) * 25);
  lastFrame = now;
  if (renderer) {
    for (const visual of objects.values()) {
      visual.group.position.lerp(visual.targetPosition, blend);
      visual.group.quaternion.slerp(visual.targetQuaternion, blend);
    }
    controls.update();
    renderer.render(scene, camera);
  }
  if (now - lastChart > 100) { drawChart(); lastChart = now; }
}
requestAnimationFrame(animate);

async function poll() {
  let delay = 40;
  try {
    const response = await fetch('/api/state', { cache: 'no-store', signal: AbortSignal.timeout(5000) });
    if (!response.ok) throw new Error('서버 응답 오류');
    renderState(await response.json());
  } catch (error) {
    delay = 1500;
    element('connection-status').textContent = '로컬 서버 연결 대기';
    element('record-button').disabled = true;
    console.warn('Tracker Studio update failed', error);
  }
  setTimeout(poll, delay);
}
poll();
