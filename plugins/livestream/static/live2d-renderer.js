/**
 * Live2D 渲染器。
 * 基于 pixi-live2d-display 渲染 Live2D 模型，
 * 接收后端 WebSocket 指令控制表情和口型。
 */
const Live2DRenderer = (function () {
    'use strict';

    let app = null;
    let model = null;
    let mouthOpen = false;
    let mouthValue = 0;
    let animFrame = null;

    // 默认模型（Haru 免费示例）
    const DEFAULT_MODEL = 'https://cdn.jsdelivr.net/gh/dylanNew/live2d/webgl/Live2D/lib/live2d.min.js';
    const MODEL_URL = 'https://cdn.jsdelivr.net/gh/Eikanya/Live2d-model/Live2D/Senko%20Normals/senko.model3.json';

    function init(canvasId) {
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;

        // 检查 PIXI 是否可用
        if (typeof PIXI === 'undefined') {
            console.warn('[Live2D] PIXI.js 未加载，使用占位渲染');
            renderPlaceholder(canvas);
            return;
        }

        try {
            app = new PIXI.Application({
                view: canvas,
                autoStart: true,
                resizeTo: window,
                backgroundAlpha: 0,
                antialias: true,
            });

            loadModel();
        } catch (e) {
            console.warn('[Live2D] 初始化失败:', e);
            renderPlaceholder(canvas);
        }
    }

    async function loadModel() {
        if (!app || typeof PIXI.live2d === 'undefined') {
            console.warn('[Live2D] pixi-live2d-display 未加载');
            return;
        }

        try {
            model = await PIXI.live2d.Live2DModel.from(MODEL_URL, {
                autoInteract: false,
                autoUpdate: true,
            });

            app.stage.addChild(model);

            // 缩放和定位
            const scale = Math.min(
                (window.innerHeight * 0.8) / model.height,
                (window.innerWidth * 0.6) / model.width
            );
            model.scale.set(scale);
            model.x = (window.innerWidth - model.width * scale) / 2;
            model.y = window.innerHeight - model.height * scale;

            // 启动口型动画循环
            startMouthAnimation();

            console.log('[Live2D] 模型加载成功');
        } catch (e) {
            console.warn('[Live2D] 模型加载失败:', e);
        }
    }

    function startMouthAnimation() {
        function animate() {
            if (model && model.internalModel) {
                const coreModel = model.internalModel.coreModel;
                // 口型参数
                if (mouthOpen) {
                    // 模拟说话时的口型波动
                    mouthValue = 0.3 + Math.random() * 0.7;
                } else {
                    mouthValue *= 0.8; // 平滑关闭
                    if (mouthValue < 0.05) mouthValue = 0;
                }
                try {
                    coreModel.setParameterValueById('ParamMouthOpenY', mouthValue);
                } catch (e) {
                    // 某些模型可能没有此参数
                }
            }
            animFrame = requestAnimationFrame(animate);
        }
        animate();
    }

    function renderPlaceholder(canvas) {
        // 当 Live2D 不可用时，渲染简单的占位动画
        const ctx = canvas.getContext('2d');
        if (!ctx) return;
        canvas.width = window.innerWidth;
        canvas.height = window.innerHeight;

        let phase = 0;
        function draw() {
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            phase += 0.02;

            // 简单的呼吸圆形
            const cx = canvas.width / 2;
            const cy = canvas.height / 2;
            const r = 80 + Math.sin(phase) * 10;

            ctx.beginPath();
            ctx.arc(cx, cy, r, 0, Math.PI * 2);
            ctx.fillStyle = 'rgba(137, 180, 250, 0.3)';
            ctx.fill();
            ctx.strokeStyle = 'rgba(137, 180, 250, 0.8)';
            ctx.lineWidth = 2;
            ctx.stroke();

            // 口型指示
            if (mouthOpen) {
                ctx.beginPath();
                ctx.arc(cx, cy + 30, 15 + mouthValue * 10, 0, Math.PI);
                ctx.fillStyle = 'rgba(255, 150, 150, 0.6)';
                ctx.fill();
            }

            requestAnimationFrame(draw);
        }
        draw();
    }

    // 接收后端指令
    function handleCommand(data) {
        switch (data.type) {
            case 'expression':
                setExpression(data.emotion, data.param);
                break;
            case 'mouth':
                setMouthOpen(data.open);
                if (data.value !== undefined) mouthValue = data.value;
                break;
            case 'action':
                triggerAction(data.action);
                break;
            case 'idle_animation':
                triggerIdleAnimation(data.action);
                break;
        }
    }

    function setExpression(emotion, param) {
        if (!model || !model.internalModel) return;
        try {
            const motionMgr = model.internalModel.motionManager;
            // 尝试通过表情管理器设置
            if (model.internalModel.expressionManager) {
                model.internalModel.expressionManager.setExpression(param);
            }
        } catch (e) {
            // 静默失败
        }
    }

    function setMouthOpen(open) {
        mouthOpen = open;
        if (!open) mouthValue = 0;
    }

    function triggerAction(action) {
        if (!model) return;
        // 简单的动作模拟
        switch (action) {
            case 'nod':
                if (model.rotation) model.rotation.x = 0.05;
                setTimeout(() => { if (model) model.rotation.x = 0; }, 300);
                break;
        }
    }

    function triggerIdleAnimation(action) {
        if (!model || !model.internalModel) return;
        try {
            const coreModel = model.internalModel.coreModel;
            switch (action) {
                case 'blink':
                    coreModel.setParameterValueById('ParamEyeLOpen', 0);
                    coreModel.setParameterValueById('ParamEyeROpen', 0);
                    setTimeout(() => {
                        if (model && model.internalModel) {
                            coreModel.setParameterValueById('ParamEyeLOpen', 1);
                            coreModel.setParameterValueById('ParamEyeROpen', 1);
                        }
                    }, 150);
                    break;
                case 'tilt_head':
                    coreModel.setParameterValueById('ParamAngleZ', 10);
                    setTimeout(() => {
                        if (model && model.internalModel) {
                            coreModel.setParameterValueById('ParamAngleZ', 0);
                        }
                    }, 500);
                    break;
            }
        } catch (e) {
            // 某些模型参数名不同
        }
    }

    return { init, handleCommand, setMouthOpen, setExpression };
})();
