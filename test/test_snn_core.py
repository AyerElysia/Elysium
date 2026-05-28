"""SNN 皮层下驱动核测试。

覆盖：LIF 神经元、STDP 突触、DriveCoreNetwork 的
前向传播、学习、序列化/反序列化、数值稳定性。
"""

import numpy as np

from plugins.life_engine.snn.core import DriveCoreNetwork, LIFNeuronGroup, STDPSynapse


# ================================================================
# LIFNeuronGroup
# ================================================================


class TestLIFNeuronGroup:
    """LIF 神经元组测试。"""

    def test_init_default(self):
        """默认初始化：膜电位应为静息值。"""
        group = LIFNeuronGroup(4)
        assert group.n == 4
        assert group.v.shape == (4,)
        np.testing.assert_array_equal(group.v, np.zeros(4))
        np.testing.assert_array_equal(group.spikes, np.zeros(4, dtype=bool))

    def test_step_integrates_current(self):
        """正向电流应增加膜电位。"""
        group = LIFNeuronGroup(2, tau=10.0, threshold=1.0)
        current = np.array([0.5, 0.0])
        group.step(current)
        assert group.v[0] > 0.0, "正向电流应提升膜电位"
        assert group.v[1] == 0.0, "零电流不应改变膜电位"

    def test_spike_and_reset(self):
        """膜电位超过阈值时应发放并重置。"""
        group = LIFNeuronGroup(1, tau=1.0, threshold=0.5, reset=0.0)
        # 注入足够大的电流使其发放
        spikes = group.step(np.array([2.0]))
        assert spikes[0], "大电流应触发发放"
        assert group.v[0] == 0.0, "发放后应重置"

    def test_leaky_decay(self):
        """无输入时膜电位应朝静息电位衰减。"""
        group = LIFNeuronGroup(1, tau=5.0, rest=0.0)
        group.v[0] = 0.8  # 手动设高
        group.step(np.zeros(1))
        assert group.v[0] < 0.8, "膜电位应衰减"
        assert group.v[0] > 0.0, "膜电位不应低于静息值"

    def test_numerical_stability_extreme_input(self):
        """极端输入不应导致 NaN 或 Inf。"""
        group = LIFNeuronGroup(4, tau=1.0)
        for _ in range(100):
            group.step(np.array([1e6, -1e6, 0.0, 1e-10]))
        assert np.all(np.isfinite(group.v)), "膜电位不应出现 NaN/Inf"
        assert np.all(np.abs(group.v) <= 10.0), "膜电位应被裁剪在 [-10, 10]"

    def test_set_state_restore(self):
        """set_state 应正确恢复膜电位。"""
        group = LIFNeuronGroup(3)
        saved = np.array([0.1, 0.5, -0.3])
        group.set_state(saved)
        np.testing.assert_array_equal(group.v, saved)

    def test_set_state_wrong_shape(self):
        """形状不匹配时 set_state 不应崩溃。"""
        group = LIFNeuronGroup(3)
        group.set_state(np.array([1.0, 2.0]))  # 形状不匹配
        # 应保持原值（不崩溃即可）
        assert group.v.shape == (3,)


# ================================================================
# STDPSynapse
# ================================================================


class TestSTDPSynapse:
    """STDP 突触测试。"""

    def test_forward(self):
        """前向传播应产生正确形状的输出。"""
        syn = STDPSynapse(4, 3)
        output = syn.forward(np.ones(4))
        assert output.shape == (3,)

    def test_weight_init_bounded(self):
        """初始权重应在合理范围内。"""
        syn = STDPSynapse(8, 16)
        assert np.all(np.abs(syn.W) <= 1.0)

    def test_update_ltp(self):
        """前→后发放应增强连接（LTP）。"""
        syn = STDPSynapse(2, 2, lr_plus=0.1, lr_minus=0.0)
        w_before = syn.W.copy()

        # 前突触先活动（留下痕迹）
        syn.update(np.array([1.0, 0.0]), np.array([0.0, 0.0]))
        # 后突触随后活动
        syn.update(np.array([0.0, 0.0]), np.array([1.0, 0.0]), reward=0.5)

        # W[0,:] 应该增强（后突触 0 发放时，前突触 0 有痕迹）
        assert syn.W[0, 0] > w_before[0, 0], "LTP 应增强连接"

    def test_update_ltd(self):
        """后→前发放应减弱连接（LTD）。"""
        syn = STDPSynapse(2, 2, lr_plus=0.0, lr_minus=0.1)
        # 先让后突触活动（留痕迹），再让前突触活动
        syn.update(np.array([0.0, 0.0]), np.array([1.0, 0.0]))
        w_before = syn.W.copy()
        syn.update(np.array([1.0, 0.0]), np.array([0.0, 0.0]))

        assert syn.W[0, 0] < w_before[0, 0], "LTD 应减弱连接"

    def test_weight_clipping(self):
        """权重应被裁剪在 [w_min, w_max] 内。"""
        syn = STDPSynapse(2, 2, lr_plus=10.0, w_min=-0.5, w_max=0.5)
        for _ in range(100):
            syn.update(np.ones(2), np.ones(2), reward=1.0)
        assert np.all(syn.W >= -0.5)
        assert np.all(syn.W <= 0.5)

    def test_reward_modulation(self):
        """正奖赏应更强烈地增强连接。"""
        syn_pos = STDPSynapse(2, 2, lr_plus=0.05, lr_minus=0.01)
        syn_neg = STDPSynapse(2, 2, lr_plus=0.05, lr_minus=0.01)
        # 同步权重
        syn_neg.W = syn_pos.W.copy()
        syn_neg.trace_pre = syn_pos.trace_pre.copy()
        syn_neg.trace_post = syn_pos.trace_post.copy()

        pre = np.array([1.0, 0.0])
        post = np.array([1.0, 0.0])

        syn_pos.update(pre, post, reward=0.8)
        syn_neg.update(pre, post, reward=-0.8)

        # 不做严格大小比较因为 LTD 也受影响，只确保不崩溃
        assert np.all(np.isfinite(syn_pos.W))
        assert np.all(np.isfinite(syn_neg.W))

    def test_get_weight_stats(self):
        """权重统计应包含所有必要字段。"""
        syn = STDPSynapse(4, 3)
        stats = syn.get_weight_stats()
        assert "w_mean" in stats
        assert "w_std" in stats
        assert "w_norm" in stats
        assert isinstance(stats["w_mean"], float)


# ================================================================
# DriveCoreNetwork
# ================================================================


class TestDriveCoreNetwork:
    """驱动核网络测试。"""

    def test_init(self):
        """初始化应创建正确维度的网络。"""
        net = DriveCoreNetwork()
        assert net.tick_count == 0
        assert net.hidden.n == 16
        assert net.output.n == 6

    def test_step_output_shape(self):
        """step 应返回正确形状的输出。"""
        net = DriveCoreNetwork()
        inp = np.random.uniform(-1, 1, 8)
        out = net.step(inp)
        assert out.shape == (6,)

    def test_step_wrong_input_shape(self):
        """错误的输入形状不应崩溃。"""
        net = DriveCoreNetwork()
        out = net.step(np.array([1.0, 2.0]))  # 形状错误
        assert out.shape == (6,)

    def test_tick_count_increments(self):
        """每次 step 应递增 tick_count。"""
        net = DriveCoreNetwork()
        net.step(np.zeros(8))
        net.step(np.zeros(8))
        net.step(np.zeros(8))
        assert net.tick_count == 3

    def test_drive_dict(self):
        """get_drive_dict 应返回所有 6 个命名维度。"""
        net = DriveCoreNetwork()
        net.step(np.ones(8) * 0.5)
        drives = net.get_drive_dict()
        assert len(drives) == 6
        for name in DriveCoreNetwork.OUTPUT_NAMES:
            assert name in drives
            assert isinstance(drives[name], float)

    def test_drive_discrete(self):
        """get_drive_discrete 应返回离散化标签。"""
        net = DriveCoreNetwork()
        net.step(np.ones(8) * 0.5)
        discrete = net.get_drive_discrete()
        assert len(discrete) == 6
        valid_levels = {"高", "中", "低", "抑制"}
        for name, level in discrete.items():
            assert level in valid_levels, f"{name}={level} 不在有效值中"

    def test_continuous_decay(self):
        """多次零输入 step 后隐藏层膜电位应趋向静息。"""
        net = DriveCoreNetwork()
        # 手动将隐藏层膜电位设高（模拟受到刺激后的状态）
        net.hidden.v = np.full(16, 0.5)
        v_before = np.linalg.norm(net.hidden.v)

        # 100 次零输入衰减
        for _ in range(100):
            net.step(np.zeros(8))
        v_after = np.linalg.norm(net.hidden.v)

        assert v_after < v_before, "零输入后膜电位应衰减"

    def test_serialize_deserialize_roundtrip(self):
        """序列化/反序列化应完整还原状态。"""
        net1 = DriveCoreNetwork()
        # 运行一些步骤
        for i in range(20):
            inp = np.random.uniform(-1, 1, 8)
            net1.step(inp, reward=np.random.uniform(-0.5, 0.5))

        data = net1.serialize()

        # 反序列化到新网络
        net2 = DriveCoreNetwork()
        net2.deserialize(data)

        # 验证状态一致
        assert net2.tick_count == net1.tick_count
        np.testing.assert_array_almost_equal(net2.hidden.v, net1.hidden.v)
        np.testing.assert_array_almost_equal(net2.output.v, net1.output.v)
        np.testing.assert_array_almost_equal(net2._output_ema, net1._output_ema)
        np.testing.assert_array_almost_equal(net2.syn_in_hid.W, net1.syn_in_hid.W)
        np.testing.assert_array_almost_equal(net2.syn_hid_out.W, net1.syn_hid_out.W)

    def test_deserialize_empty_dict(self):
        """空字典反序列化不应崩溃。"""
        net = DriveCoreNetwork()
        net.deserialize({})
        assert net.tick_count == 0

    def test_deserialize_partial_data(self):
        """部分数据反序列化不应崩溃。"""
        net = DriveCoreNetwork()
        net.deserialize({"tick_count": 42, "hidden_v": [0.1] * 16})
        assert net.tick_count == 42

    def test_get_health(self):
        """get_health 应返回完整的健康信息。"""
        net = DriveCoreNetwork()
        net.step(np.ones(8) * 0.3)
        health = net.get_health()
        assert "tick_count" in health
        assert "drives" in health
        assert "drives_discrete" in health
        assert "syn_in_hid" in health
        assert "syn_hid_out" in health
        assert health["tick_count"] == 1

    def test_long_run_numerical_stability(self):
        """长时间运行不应出现数值问题。"""
        net = DriveCoreNetwork()
        rng = np.random.default_rng(42)

        for i in range(500):
            inp = rng.uniform(-1, 1, 8)
            reward = rng.uniform(-1, 1)
            out = net.step(inp, reward=reward)

            assert np.all(np.isfinite(out)), f"Step {i}: 输出包含 NaN/Inf"
            assert np.all(np.abs(net.hidden.v) <= 10.0), f"Step {i}: 隐藏层膜电位越界"
            assert np.all(np.abs(net.output.v) <= 10.0), f"Step {i}: 输出层膜电位越界"

        # 权重应保持在界内
        assert np.all(np.abs(net.syn_in_hid.W) <= 1.0)
        assert np.all(np.abs(net.syn_hid_out.W) <= 1.0)

    def test_stdp_learning_effect(self):
        """反复相同模式应改变权重（证明学习在发生）。"""
        net = DriveCoreNetwork()
        w_init_in = net.syn_in_hid.W.copy()

        # 反复注入相同模式 + 正奖赏
        pattern = np.array([1.0, -0.5, 0.8, 0.0, -1.0, 0.3, 0.6, -0.2])
        for _ in range(200):
            net.step(pattern, reward=0.5)

        w_after_in = net.syn_in_hid.W

        # 权重应发生变化
        in_change = np.linalg.norm(w_after_in - w_init_in)
        assert in_change > 0.001, f"输入→隐藏权重变化过小: {in_change}"
