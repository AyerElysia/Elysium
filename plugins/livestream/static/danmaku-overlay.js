/**
 * 弹幕飘屏覆盖层。
 * 从右向左滚动显示弹幕文本。
 */
const DanmakuOverlay = (function () {
    'use strict';

    let container = null;
    let laneCount = 8;
    let laneHeights = [];
    let active = true;

    function init(containerId) {
        container = document.getElementById(containerId);
        if (!container) return;
        // 计算轨道高度
        const h = container.clientHeight || window.innerHeight * 0.6;
        const laneH = h / laneCount;
        laneHeights = Array.from({ length: laneCount }, (_, i) => i * laneH + 10);
    }

    function spawn(text) {
        if (!container || !active) return;

        const el = document.createElement('div');
        el.className = 'danmaku-item';
        el.textContent = text;

        // 随机轨道
        const lane = Math.floor(Math.random() * laneCount);
        el.style.top = laneHeights[lane] + 'px';

        // 随机速度（8-14秒穿越）
        const duration = 8 + Math.random() * 6;
        el.style.animationDuration = duration + 's';

        // 随机颜色
        const colors = ['#fff', '#ffd700', '#87ceeb', '#98fb98', '#ffb6c1'];
        el.style.color = colors[Math.floor(Math.random() * colors.length)];

        container.appendChild(el);

        // 动画结束后移除
        el.addEventListener('animationend', () => el.remove());
        // 安全超时移除
        setTimeout(() => { if (el.parentNode) el.remove(); }, (duration + 2) * 1000);
    }

    function setActive(val) {
        active = val;
        if (!val && container) {
            container.innerHTML = '';
        }
    }

    // 自动初始化
    document.addEventListener('DOMContentLoaded', () => init('danmaku-overlay'));

    return { init, spawn, setActive };
})();
