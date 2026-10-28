// 聊天里 3D 模型附件（.glb / .gltf）的查看器：点开附件时由 index.html 按需 import，three.js 在 vendor/three/（页面的 importmap 指过去）。
// 操作同 Blender 的视口：左键拖旋转、右键拖 / Shift+左键平移、滚轮缩放、F 对准；贴图 / 白模 / 线框、地面网格、自动旋转。
// open(box, url) 在 box 里建画布开始加载，返回关闭函数（停渲染、释放显存）；弹层关掉时调用它。
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";

export function open(box, url) {
  box.innerHTML = `<canvas></canvas>
    <div class="v3d-bar"><button data-m="tex" class="on">贴图</button><button data-m="clay">白模</button><button data-m="wire">线框</button>
      <button data-a="grid" class="on">地面网格</button><button data-a="rot">自动旋转</button><button data-a="frame">对准（F）</button></div>
    <div class="v3d-info"></div><div class="v3d-load">加载模型 0%</div>
    <div class="v3d-help">左键拖 旋转 · 右键拖 / Shift+左键 平移 · 滚轮 缩放 · F 对准</div>`;
  const canvas = box.querySelector("canvas"),
    load = box.querySelector(".v3d-load");
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x3a3a3a);
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  const sun = new THREE.DirectionalLight(0xffffff, 1.2);
  sun.position.set(3, 5, 4);
  scene.add(sun);
  const camera = new THREE.PerspectiveCamera(40, 1, 0.01, 100);
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.screenSpacePanning = true;
  const grid = new THREE.GridHelper(4, 40, 0x666666, 0x4a4a4a),
    axes = new THREE.AxesHelper(0.4);
  scene.add(grid, axes);
  const orig = new Map(),
    box3 = new THREE.Box3();
  const clay = new THREE.MeshStandardMaterial({
      color: 0xbfbfbf,
      roughness: 0.8,
    }),
    wire = new THREE.MeshBasicMaterial({ color: 0xe8e8e8, wireframe: true });
  let model = null;

  function frame() {
    const c = box3.getCenter(new THREE.Vector3()),
      r = box3.getSize(new THREE.Vector3()).length() / 2 || 1;
    const dist = (r / Math.tan(THREE.MathUtils.degToRad(camera.fov / 2))) * 1.1;
    controls.target.copy(c);
    camera.position
      .copy(c)
      .add(new THREE.Vector3(0.6, 0.35, 1).normalize().multiplyScalar(dist));
    camera.near = dist / 100;
    camera.far = dist * 100;
    camera.updateProjectionMatrix();
    controls.update();
  }
  new GLTFLoader().load(
    url,
    (g) => {
      model = g.scene;
      scene.add(model);
      box3.setFromObject(model);
      model.position.y -= box3.min.y; // 放到地面上
      box3.setFromObject(model);
      let tris = 0;
      model.traverse((o) => {
        if (o.isMesh) {
          orig.set(o, o.material);
          tris +=
            (o.geometry.index
              ? o.geometry.index.count
              : o.geometry.attributes.position.count) / 3;
        }
      });
      box.querySelector(".v3d-info").textContent =
        `${Math.round(tris).toLocaleString()} 个三角面`;
      load.remove();
      frame();
    },
    (e) => {
      if (e.total)
        load.textContent = `加载模型 ${Math.round((e.loaded / e.total) * 100)}%`;
    },
    (e) => {
      load.textContent = `加载失败：${e.message || e}`;
    },
  );
  box.querySelectorAll("[data-m]").forEach(
    (b) =>
      (b.onclick = () => {
        box
          .querySelectorAll("[data-m]")
          .forEach((x) => x.classList.toggle("on", x === b));
        model?.traverse((o) => {
          if (o.isMesh)
            o.material =
              b.dataset.m === "tex"
                ? orig.get(o)
                : b.dataset.m === "clay"
                  ? clay
                  : wire;
        });
      }),
  );
  box.querySelector('[data-a="grid"]').onclick = (e) => {
    grid.visible = axes.visible = !grid.visible;
    e.target.classList.toggle("on", grid.visible);
  };
  box.querySelector('[data-a="rot"]').onclick = (e) => {
    controls.autoRotate = !controls.autoRotate;
    e.target.classList.toggle("on", controls.autoRotate);
  };
  box.querySelector('[data-a="frame"]').onclick = frame;
  const key = (e) => {
    if (
      (e.key === "f" || e.key === "F") &&
      !e.target.closest?.("input, textarea")
    )
      frame();
  };
  addEventListener("keydown", key);
  const fit = () => {
    const w = box.clientWidth,
      h = box.clientHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  };
  const ro = new ResizeObserver(fit);
  ro.observe(box);
  fit();
  renderer.setAnimationLoop(() => {
    controls.update();
    renderer.render(scene, camera);
  });

  return () => {
    renderer.setAnimationLoop(null);
    ro.disconnect();
    removeEventListener("keydown", key);
    controls.dispose();
    orig.forEach((m, o) => (o.material = m)); // 换回原材质，下面连同贴图一起释放
    scene.traverse((o) => {
      o.geometry?.dispose();
      for (const m of [o.material].flat()) {
        if (!m) continue;
        for (const v of Object.values(m)) if (v?.isTexture) v.dispose();
        m.dispose();
      }
    });
    clay.dispose();
    wire.dispose();
    pmrem.dispose();
    scene.environment?.dispose();
    renderer.dispose();
  };
}
