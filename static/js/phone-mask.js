// Маска для узбекских номеров: на экране +998 (50)-002-21-31, на сервер уходит как есть,
// а сервер сам приводит номер к виду +998500022131.
(function () {
    const PREFIX = '+998 ';

    function localDigits(value) {
        let digits = value.replace(/\D/g, '');
        // Префикс 998 отрезаем, если он уже был в поле или номер вставили целиком (12 цифр)
        if (digits.startsWith('998') && (value.trim().startsWith('+') || digits.length > 9)) {
            digits = digits.slice(3);
        }
        return digits.slice(0, 9);
    }

    function format(d) {
        if (!d.length) return PREFIX;
        let out = PREFIX + '(' + d.slice(0, 2);
        if (d.length >= 2) out += ')';
        if (d.length > 2) out += '-' + d.slice(2, 5);
        if (d.length > 5) out += '-' + d.slice(5, 7);
        if (d.length > 7) out += '-' + d.slice(7, 9);
        return out;
    }

    function attach(input) {
        let previous = localDigits(input.value);
        if (input.value) input.value = format(previous);

        input.addEventListener('focus', function () {
            if (!input.value) input.value = PREFIX;
        });
        input.addEventListener('blur', function () {
            if (input.value === PREFIX) input.value = '';
        });
        input.addEventListener('input', function (event) {
            let digits = localDigits(input.value);
            // Стёрли скобку или дефис — стираем и цифру перед ним, иначе курсор «застревает»
            if (event.inputType === 'deleteContentBackward' && digits === previous && digits.length) {
                digits = digits.slice(0, -1);
            }
            previous = digits;
            input.value = format(digits);
            input.setSelectionRange(input.value.length, input.value.length);
        });
    }

    document.querySelectorAll('input.phone-input').forEach(attach);
})();
