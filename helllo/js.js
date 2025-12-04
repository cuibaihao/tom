// main.js
window.addEventListener('DOMContentLoaded', function () {
    // 获取表单和密码输入框
    const form = document.querySelector('form');
    const passwordInput = document.querySelector('input[type="password"]');

    form.addEventListener('submit', function (e) {
        const pwd = passwordInput.value;

        if (pwd.length < 8) {
            // 阻止表单提交
            e.preventDefault();
            alert('密码长度必须大于等于 8 位！');
            passwordInput.focus();
        }
    });
});