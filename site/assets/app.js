/* 共享工具：数据读取、格式化、图表基础样式 */

/* 数据根目录，按顺序尝试，取第一个能读到的：
 * 1. ../data/public —— 本地跑过 --run 后的真实产物（已 gitignore，不进仓库）
 * 2. ../data/demo   —— 仓库自带的合成演示数据（布局：仓库根目录起服务）
 * 3. data/public    —— 把 site/ 作为发布根时的真实产物
 * 4. data/demo      —— 把 site/ 作为发布根时的演示数据
 * 这样换发布方式或数据存在与否都不会白屏。
 */
const DATA_ROOTS = ["../data/public", "../data/demo", "data/public", "data/demo"];
let DATA_ROOT = null;

const COLORS = {
  pos: "#3ecf8e",
  neg: "#ff5d5d",
  accent: "#4c8dff",
  muted: "#8b93a7",
  text: "#e6e9ef",
};

const TOOLTIP = {
  backgroundColor: "#1e222b",
  borderColor: "#272c37",
  textStyle: { color: COLORS.text },
};

const AXIS_LINE = { lineStyle: { color: "#272c37" } };
const AXIS_LABEL = { color: COLORS.muted };
const AXIS_SPLIT = { lineStyle: { color: "#1e222b" } };

async function loadJson(relPath) {
  const roots = DATA_ROOT ? [DATA_ROOT] : DATA_ROOTS;
  let lastError = null;

  for (const root of roots) {
    try {
      const response = await fetch(`${root}/${relPath}`);
      if (!response.ok) {
        lastError = new Error(`读取 ${root}/${relPath} 失败：HTTP ${response.status}`);
        continue;
      }
      DATA_ROOT = root;
      return await response.json();
    } catch (error) {
      lastError = error;
    }
  }

  throw lastError || new Error(`读取 ${relPath} 失败`);
}

function categoryAxis(data) {
  return {
    type: "category",
    data,
    axisLine: AXIS_LINE,
    axisLabel: AXIS_LABEL,
    axisTick: { show: false },
  };
}

function valueAxis(extra) {
  return Object.assign({
    type: "value",
    axisLine: AXIS_LINE,
    axisLabel: AXIS_LABEL,
    splitLine: AXIS_SPLIT,
  }, extra || {});
}

function mountChart(id) {
  return echarts.init(document.getElementById(id), null, { renderer: "canvas" });
}

function pct(value) {
  return `${Math.round(value * 100)}%`;
}

function scoreClass(value) {
  if (value > 0.05) return "pos";
  if (value < -0.05) return "neg";
  return "";
}

function formatTime(seconds) {
  return new Date(seconds * 1000).toLocaleString("zh-CN", {
    hour12: false, timeZone: "Asia/Shanghai",
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

function formatDate(iso) {
  return iso.slice(5);
}

function formatDay(iso) {
  return iso.slice(5, 10);
}

function statCard(label, value, cls) {
  return `<div class="stat"><div class="k">${label}</div>
    <div class="v ${cls || ""}">${value}</div></div>`;
}

function showError(target, error) {
  const node = document.getElementById(target);
  if (node) {
    node.innerHTML = `<div class="empty">加载失败：${error.message}<br>
      请用本地服务器打开页面（例如 <code>python -m http.server</code>），
      直接双击 html 文件会被浏览器的跨域策略拦住。</div>`;
  }
  console.error(error);
}
