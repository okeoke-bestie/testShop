/* ============================================================
   GAMEPORT — витрина

   Без фреймворка и без сборки: файл открывается как есть.
   Состояние живёт в одном объекте, отрисовка идёт от него —
   тот же принцип, что во React, только руками.
   ============================================================ */

'use strict';

const API = '/api';

const state = {
  page: 'catalog',
  productSku: null,     // какой товар открыт
  product: null,        // данные его страницы
  customer: null,       // вошедший покупатель
  token: null,          // токен сессии
  me: null,             // заказы и покупки профиля
  products: [],
  categories: [],
  platforms: [],
  category: 'all',
  platform: 'all',
  reviews: [],
  reviewSummary: null,
  search: '',
  sort: 'featured',
  cart: new Map(),   // product_id -> quantity
  lastOrderId: null,
  backend: '—',
  loading: true,
};

/* ---------------------------------------------- утилиты */

const $ = (id) => document.getElementById(id);

/** Копейки в строку с разделителями разрядов.
 *  Деньги приходят с сервера целым числом копеек — на клиенте
 *  их только форматируют, но не считают: арифметика с деньгами
 *  остаётся на сервере, где нет плавающей точки. */
function money(kopecks) {
  const rub = Math.trunc(kopecks / 100);
  const kop = Math.abs(kopecks % 100);
  const grouped = rub.toLocaleString('ru-RU');
  return kop ? `${grouped},${String(kop).padStart(2, '0')} ₽` : `${grouped} ₽`;
}

/** Экранирование: тексты приходят из базы, а их туда кладёт человек.
 *  Вставлять их в innerHTML без экранирования — это XSS. */
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

/* Раньше здесь лежала палитра градиентов: обложек не было, и карточка
 * закрашивалась цветом, выведенным из артикула. Теперь у каждого товара
 * есть своя картинка в /static/art/<sku>.svg — её рисует скрипт
 * scripts/make_art.py. Градиент остался только как запасной фон под
 * картинкой: пока она грузится, карточка не должна быть белой дырой. */

/** Обычный клик левой кнопкой без модификаторов.
 *  Всё остальное оставляем браузеру: Ctrl+клик, средняя кнопка
 *  и «открыть в новой вкладке» должны работать как везде. */
function isPlainClick(event) {
  return !event.metaKey && !event.ctrlKey && !event.shiftKey
      && !event.altKey && event.button === 0;
}

function plural(n, one, few, many) {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few;
  return many;
}

function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

/* ---------------------------------------------- уведомления */

function toast(message, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind ? 'toast--' + kind : ''}`;
  el.textContent = message;
  $('toasts').appendChild(el);
  setTimeout(() => {
    el.classList.add('is-leaving');
    el.addEventListener('animationend', () => el.remove(), { once: true });
  }, 2800);
}

/* ---------------------------------------------- запросы */

async function api(path, options = {}) {
  // Порядок здесь важен: сначала разворачиваем options, и только потом
  // собираем headers. В обратном порядке `...options` затирал объект
  // headers целиком вместе с Content-Type — запрос уходил как
  // text/plain, и сервер отвечал 422 на совершенно корректное тело.
  // Через curl всё работало, потому что там заголовок ставился руками.
  // Токен сессии уходит стандартным заголовком Authorization.
  // Не в теле и не в адресе: адреса целиком пишутся в журналы
  // доступа, и сессия утекла бы туда вместе с ними.
  const who = state.token ? { Authorization: `Bearer ${state.token}` } : {};
  const response = await fetch(API + path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...who,
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    // Сервер отдаёт доменную ошибку в едином формате {code, message, details}.
    // Показываем человеку message, а не «HTTP 409».
    const error = new Error(payload.message || `Ошибка ${response.status}`);
    error.code = payload.code;
    error.details = payload.details;
    error.status = response.status;
    throw error;
  }
  return payload;
}

/* ---------------------------------------------- загрузка */

async function loadCatalog() {
  state.loading = true;
  renderSkeletons();
  try {
    const params = new URLSearchParams();
    if (state.category !== 'all') params.set('category', state.category);
    if (state.platform !== 'all') params.set('platform', state.platform);
    if (state.search.trim()) params.set('search', state.search.trim());

    const data = await api('/products?' + params);
    state.products = data.items;
    state.categories = data.categories;
    state.platforms = data.platforms || [];
    state.backend = data.backend;
    state.loading = false;

    renderCategories();
    renderPlatforms();
    renderGrid();
    $('footer-backend').textContent = `база: ${data.backend}`;
    $('fact-products').textContent = data.total_stock.toLocaleString('ru-RU');
  } catch (error) {
    state.loading = false;
    $('grid').innerHTML = '';
    $('empty').hidden = false;
    $('empty').querySelector('h3').textContent = 'Каталог недоступен';
    $('empty').querySelector('p').textContent = error.message;
    toast('Не удалось загрузить каталог', 'error');
  }
}

async function loadOrders() {
  try {
    const data = await api('/orders/recent');
    $('fact-orders').textContent = String(data.total);
    if (!data.items.length) { $('orders').hidden = true; return; }

    $('orders').hidden = false;
    $('orders-list').innerHTML = data.items.map((order) => `
      <div class="order-row">
        <div>
          <div class="order-row__id">Заказ №${order.id}</div>
          <div class="order-row__meta">
            ${order.items} ${plural(order.items, 'товар', 'товара', 'товаров')}
            · ${esc(order.created_at)}
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:16px">
          <span class="status status--${esc(order.status)}">${statusLabel(order.status)}</span>
          <span class="order-row__sum">${money(order.total_kopecks)}</span>
        </div>
      </div>
    `).join('');
  } catch {
    $('orders').hidden = true;
  }
}

function statusLabel(status) {
  return { new: 'Новый', paid: 'Оплачен', shipped: 'Отправлен', cancelled: 'Отменён' }[status] || status;
}

/* ---------------------------------------------- отрисовка */

function renderSkeletons() {
  $('empty').hidden = true;
  $('grid').innerHTML = Array.from({ length: 8 }, () => `
    <div class="skeleton">
      <div class="skeleton__art"></div>
      <div class="skeleton__line"></div>
      <div class="skeleton__line skeleton__line--short"></div>
    </div>
  `).join('');
}

function renderCategories() {
  const total = state.categories.reduce((sum, c) => sum + c.count, 0);
  const chips = [{ category: 'all', count: total }, ...state.categories];

  $('categories').innerHTML = chips.map((c) => `
    <button class="chip ${state.category === c.category ? 'is-active' : ''}"
            data-category="${esc(c.category)}" type="button" role="tab"
            aria-selected="${state.category === c.category}">
      ${c.category === 'all' ? 'Все товары' : esc(c.category)}
      <span class="chip__count">${c.count}</span>
    </button>
  `).join('');
}

/** Строка с оценкой. Товар без отзывов её не показывает вовсе:
 *  «0,0 ★ (0)» выглядит как плохая оценка, хотя оценки просто нет. */
function ratingLine(p) {
  if (!p.reviews_count) return '<div class="card__rating card__rating--none">нет отзывов</div>';
  return `<div class="card__rating">
            <span class="card__stars" aria-hidden="true">${stars(Math.round(p.rating))}</span>
            <span>${String(p.rating).replace('.', ',')}</span>
            <span class="card__rating-count">${p.reviews_count}</span>
          </div>`;
}

function renderPlatforms() {
  const total = state.platforms.reduce((sum, p) => sum + p.count, 0);
  const chips = [{ platform: 'all', count: total }, ...state.platforms];

  $('platforms').innerHTML = chips.map((p) => `
    <button class="chip chip--platform ${state.platform === p.platform ? 'is-active' : ''}"
            data-platform="${esc(p.platform)}" type="button" role="tab"
            aria-selected="${state.platform === p.platform}">
      ${p.platform === 'all' ? 'Все платформы' : esc(p.platform)}
      <span class="chip__count">${p.count}</span>
    </button>
  `).join('');
}

// Порядок категорий на витрине. По алфавиту первыми оказывались
// геймпады — для магазина это странная первая полка. Здесь порядок
// задан явно: сначала консоли, потом игры, потом аксессуары.
const CATEGORY_ORDER = { 'Консоли': 0, 'Игры': 1, 'Аксессуары': 2 };

function byTitle(a, b) { return a.title.localeCompare(b.title, 'ru'); }

function sortedProducts() {
  const items = [...state.products];
  switch (state.sort) {
    case 'price-asc': return items.sort((a, b) => a.price_kopecks - b.price_kopecks);
    case 'price-desc': return items.sort((a, b) => b.price_kopecks - a.price_kopecks);
    case 'stock': return items.sort((a, b) => b.stock - a.stock);
    case 'year': return items.sort((a, b) => (b.year || 0) - (a.year || 0) || byTitle(a, b));
    case 'title': return items.sort(byTitle);
    default:
      return items.sort((a, b) => {
        const byCategory = (CATEGORY_ORDER[a.category] ?? 9) - (CATEGORY_ORDER[b.category] ?? 9);
        return byCategory || byTitle(a, b);
      });
  }
}

function renderGrid() {
  const items = sortedProducts();
  $('empty').hidden = items.length > 0;
  const where = [
    state.category !== 'all' ? `в категории «${state.category}»` : '',
    state.platform !== 'all' ? `для ${state.platform}` : '',
  ].filter(Boolean).join(' ');
  $('catalog-summary').textContent = items.length
    ? `${items.length} ${plural(items.length, 'товар', 'товара', 'товаров')}` +
      (where ? ` ${where}` : '')
    : 'Ничего не найдено';

  $('grid').innerHTML = items.map((p, index) => {
    const inCart = state.cart.get(p.id) || 0;
    const available = p.stock - inCart;
    const out = p.stock === 0;

    let badge = '';
    if (out) badge = '<span class="card__badge card__badge--out">Нет в наличии</span>';
    else if (p.stock <= 3) badge = `<span class="card__badge card__badge--low">Осталось ${p.stock}</span>`;
    else if (p.stock >= 50) badge = '<span class="card__badge card__badge--hot">Много в наличии</span>';

    // Подпись под названием: год и студия для игр, платформа
    // для консолей и аксессуаров. Пустые куски отсеиваются, иначе
    // на экране появляются висячие точки-разделители.
    const meta = [p.platform, p.year || null, p.developer]
      .filter(Boolean).map(esc).join(' · ');

    return `
      <article class="card ${out ? 'is-out' : ''}" data-card="${p.id}"
               style="animation-delay:${Math.min(index * 35, 400)}ms">
        <a class="card__link" href="/product/${esc(p.sku)}" data-product="${esc(p.sku)}"
           aria-label="${esc(p.title)}"></a>
        <div class="card__art">
          <img class="card__cover" src="${esc(p.art)}" alt="Обложка: ${esc(p.title)}"
               width="640" height="800" loading="lazy" decoding="async">
          ${p.genre ? `<span class="card__genre">${esc(p.genre)}</span>` : ''}
          ${badge}
        </div>
        <div class="card__body">
          <h3 class="card__title">${esc(p.title)}</h3>
          <div class="card__meta">${meta || esc(p.category)}</div>
          ${ratingLine(p)}
          <p class="card__desc">${esc(p.description)}</p>
          <div class="card__foot">
            <div>
              <div class="card__price">${money(p.price_kopecks)}</div>
              <div class="card__stock">${out ? 'Закончился' : `${available} шт. доступно`}</div>
            </div>
            <button class="btn btn--primary btn--sm" data-add="${p.id}"
                    ${out || available <= 0 ? 'disabled' : ''}>
              ${out ? 'Нет' : (inCart ? 'Ещё' : 'В корзину')}
            </button>
          </div>
        </div>
      </article>
    `;
  }).join('');
}

function renderCart() {
  const lines = [...state.cart.entries()].map(([id, qty]) => {
    const product = state.products.find((p) => p.id === id);
    return product ? { product, qty } : null;
  }).filter(Boolean);

  const count = lines.reduce((sum, l) => sum + l.qty, 0);
  const badge = $('cart-count');
  badge.hidden = count === 0;
  if (count) {
    badge.textContent = String(count);
    badge.classList.remove('is-bump');
    void badge.offsetWidth;   // перезапуск анимации: без этого класс
    badge.classList.add('is-bump');  // навешивается, а анимация не идёт
  }

  const empty = lines.length === 0;
  $('cart-empty').hidden = !empty;
  $('cart-foot').hidden = empty;

  $('cart-lines').innerHTML = lines.map(({ product, qty }) => `
    <li class="cart-line" data-line="${product.id}">
      <img class="cart-line__art" src="/static/art/${esc(product.sku)}.svg"
           alt="" width="640" height="800" loading="lazy">
      <div>
        <div class="cart-line__title">${esc(product.title)}</div>
        <div class="cart-line__price">${money(product.price_kopecks)} за штуку</div>
      </div>
      <div class="cart-line__right">
        <div class="stepper">
          <button data-dec="${product.id}" aria-label="Меньше">−</button>
          <span class="stepper__value">${qty}</span>
          <button data-inc="${product.id}" aria-label="Больше"
                  ${qty >= product.stock ? 'disabled' : ''}>+</button>
        </div>
        <strong>${money(product.price_kopecks * qty)}</strong>
      </div>
    </li>
  `).join('');

  const subtotal = lines.reduce((sum, l) => sum + l.product.price_kopecks * l.qty, 0);
  const shipping = subtotal >= 500000 || subtotal === 0 ? 0 : 39000;

  $('total-items').textContent = `${count} ${plural(count, 'штука', 'штуки', 'штук')}`;
  $('total-shipping').textContent = shipping ? money(shipping) : 'бесплатно';
  $('total-sum').textContent = money(subtotal + shipping);
}

/* ---------------------------------------------- корзина */

function addToCart(productId) {
  const product = state.products.find((p) => p.id === productId);
  if (!product) return;

  const current = state.cart.get(productId) || 0;
  if (current + 1 > product.stock) {
    toast(`Больше нет: на складе ${product.stock} шт.`, 'error');
    return;
  }
  state.cart.set(productId, current + 1);
  renderCart();
  // Раньше здесь стоял renderGrid(): он пересобирал innerHTML всей
  // сетки, из-за чего сорок три карточки исчезали и появлялись заново.
  // Браузер перезапрашивал обложки, заново проигрывал анимацию
  // появления, а прокрутка прыгала. Правильный путь — поправить
  // ровно то, что изменилось: остаток и кнопку одной карточки.
  refreshCard(productId);

  const button = document.querySelector(`[data-add="${productId}"]`);
  if (button) {
    button.classList.add('is-added');
    button.textContent = 'Добавлено';
    setTimeout(() => {
      button.classList.remove('is-added');
      refreshCard(productId);
    }, 900);
  }
}

/** Обновляет ОДНУ карточку на месте, не трогая остальные.
 *
 *  Это ручной аналог того, что во фреймворках делает сверка
 *  виртуального дерева: посчитать разницу и применить только её.
 *  Здесь состояние простое, и хватает нескольких строк. */
function refreshCard(productId) {
  const product = state.products.find((p) => p.id === productId);
  if (!product) return;

  const inCart = state.cart.get(productId) || 0;
  const available = product.stock - inCart;
  const out = product.stock === 0;

  document.querySelectorAll(`[data-card="${productId}"]`).forEach((card) => {
    const stock = card.querySelector('.card__stock');
    if (stock) {
      stock.textContent = out
        ? 'Закончился'
        : `${available} шт. доступно`;
    }
    const button = card.querySelector('[data-add]');
    if (button && !button.classList.contains('is-added')) {
      button.disabled = out || available <= 0;
      button.textContent = out ? 'Нет' : (inCart ? 'Ещё' : 'В корзину');
    }
    card.classList.toggle('is-out', out);
  });

  // Кнопка на странице товара — тот же товар, другая разметка.
  const pageButton = document.querySelector('#product-body [data-add]');
  if (pageButton && Number(pageButton.dataset.add) === productId) {
    pageButton.disabled = out || available <= 0;
    pageButton.textContent = out ? 'Нет в наличии' : (inCart ? 'Добавить ещё' : 'В корзину');
  }
}

function changeQty(productId, delta) {
  const product = state.products.find((p) => p.id === productId);
  const next = (state.cart.get(productId) || 0) + delta;

  if (next <= 0) {
    const line = document.querySelector(`[data-line="${productId}"]`);
    if (line) line.classList.add('is-leaving');
    state.cart.delete(productId);
    setTimeout(() => { renderCart(); refreshCard(productId); }, 160);
    return;
  }
  if (product && next > product.stock) {
    toast(`На складе только ${product.stock} шт.`, 'error');
    return;
  }
  state.cart.set(productId, next);
  renderCart();
  refreshCard(productId);
}

function openCart() {
  $('cart').classList.add('is-open');
  $('cart').setAttribute('aria-hidden', 'false');
  $('backdrop').hidden = false;
  requestAnimationFrame(() => $('backdrop').classList.add('is-open'));
  document.body.style.overflow = 'hidden';
  $('cart-close').focus();
}

function closeCart() {
  $('cart').classList.remove('is-open');
  $('cart').setAttribute('aria-hidden', 'true');
  $('backdrop').classList.remove('is-open');
  setTimeout(() => { $('backdrop').hidden = true; }, 260);
  document.body.style.overflow = '';
  $('success').hidden = true;
}

/* ---------------------------------------------- оформление */

async function checkout() {
  if (!state.cart.size) return;

  const button = $('checkout');
  button.disabled = true;
  button.textContent = 'Оформляем…';

  // Ключ идемпотентности генерируется ОДИН раз на попытку оформления.
  // Если ответ потеряется и пользователь нажмёт снова, сервер вернёт
  // тот же заказ, а не создаст второй.
  const idempotencyKey = (crypto.randomUUID?.() || String(Date.now() + Math.random()));

  try {
    const order = await api('/orders', {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify({
        customer_id: 1,
        items: [...state.cart.entries()].map(([product_id, quantity]) => ({
          product_id, quantity,
        })),
      }),
    });

    state.lastOrderId = order.id;
    state.cart.clear();
    $('success-id').textContent = String(order.id);
    $('success').hidden = false;

    await Promise.all([loadCatalog(), loadOrders()]);
    renderCart();
  } catch (error) {
    if (error.code === 'out_of_stock') {
      toast(error.message, 'error');
      // Остатки изменились, пока пользователь думал — перечитываем.
      await loadCatalog();
      renderCart();
    } else {
      toast(error.message, 'error');
    }
  } finally {
    button.disabled = false;
    button.textContent = 'Оформить заказ';
  }
}

async function cancelLastOrder() {
  if (!state.lastOrderId) return;
  try {
    await api(`/orders/${state.lastOrderId}/cancel`, { method: 'POST' });
    toast('Заказ отменён, товары вернулись на склад', 'success');
    state.lastOrderId = null;
    closeCart();
    await Promise.all([loadCatalog(), loadOrders()]);
  } catch (error) {
    toast(error.message, 'error');
  }
}


/* ---------------------------------------------- роутер

   Разделы переключаются без перезагрузки, но адреса настоящие:
   /reviews, /contacts и так далее. Это History API, а не якоря
   вида #reviews — и разница здесь не косметическая:

     * ссылку можно прислать, и она откроет нужный раздел;
     * работают «назад» и «вперёд» в браузере;
     * адрес видно в строке, его можно скопировать.

   Плата за это — сервер обязан отдавать страницу на КАЖДЫЙ из этих
   адресов. Пока ходишь по сайту, запросов к серверу нет, и всё
   работает само; но стоит нажать F5 на /reviews — браузер спросит
   этот путь у сервера по-настоящему. Без обработчика там будет 404,
   и поломка вылезет не у разработчика, а у пользователя, который
   обновил страницу. В api.py для этого есть отдельная ручка. */

const PAGES = ['catalog', 'reviews', 'delivery', 'contacts', 'about', 'profile'];

/** Разбирает адрес в раздел и, для товара, в артикул.
 *
 *  Возвращает пару, а не просто имя: у страницы товара адрес состоит
 *  из двух частей, и вытаскивать артикул повторно в трёх местах —
 *  верный способ однажды разойтись. */
function routeFromPath(pathname) {
  const parts = pathname.replace(/^\/+|\/+$/g, '').split('/');
  if (parts[0] === 'product' && parts[1]) {
    return { page: 'product', sku: decodeURIComponent(parts[1]) };
  }
  return { page: PAGES.includes(parts[0]) ? parts[0] : 'catalog', sku: null };
}

function showPage(page, { push = true, scroll = true, sku = null } = {}) {
  state.page = page;

  document.querySelectorAll('[data-page]').forEach((section) => {
    section.hidden = section.dataset.page !== page;
  });
  document.querySelectorAll('[data-nav]').forEach((link) => {
    const active = link.dataset.nav === page;
    link.classList.toggle('is-active', active);
    // aria-current говорит экранному диктору, где мы находимся;
    // подсветки цветом для этого недостаточно.
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });

  // Поиск относится только к каталогу — на других разделах он лишний.
  $('search').closest('.search').hidden = page !== 'catalog';

  let url = '/';
  if (page === 'product' && sku) url = `/product/${encodeURIComponent(sku)}`;
  else if (page !== 'catalog') url = `/${page}`;

  if (push && location.pathname !== url) history.pushState({ page, sku }, '', url);
  document.title = page === 'catalog'
    ? 'GAMEPORT — магазин игр и консолей'
    : `${PAGE_TITLES[page] || 'Товар'} — GAMEPORT`;

  if (scroll) scrollTo({ top: 0, behavior: 'instant' in document.body.style ? 'instant' : 'auto' });

  if (page === 'reviews' && !state.reviews.length) loadReviews();
  // Товар перезагружается, когда сменился артикул: переход с одной
  // карточки на другую не должен показывать прошлый товар.
  if (page === 'product' && sku && sku !== state.product?.product?.sku) loadProduct(sku);
  if (page === 'profile') loadProfile();
}

const PAGE_TITLES = {
  catalog: 'Каталог', reviews: 'Отзывы', delivery: 'Доставка',
  contacts: 'Контакты', about: 'О нас', profile: 'Профиль',
};


/* ---------------------------------------------- страница товара */

async function loadProduct(sku) {
  state.productSku = sku;
  $('product-tabs').hidden = true;
  $('product-body').innerHTML = '<div class="product__skeleton">Загружаем товар…</div>';

  try {
    const data = await api(`/products/by-sku/${encodeURIComponent(sku)}`);
    state.product = data;
    renderProduct();
  } catch (error) {
    state.product = null;
    $('product-body').innerHTML = `
      <div class="empty">
        <div class="empty__art" aria-hidden="true">🔍</div>
        <h3>Товар не найден</h3>
        <p>${esc(error.message)}</p>
        <a class="btn btn--primary" href="/" data-nav="catalog">Вернуться в каталог</a>
      </div>`;
  }
}

function renderProduct() {
  const { product: p, specs, reviews, rating, recommendations } = state.product;

  document.title = `${p.title} — GAMEPORT`;
  $('crumb-category').textContent = p.category;

  const inCart = state.cart.get(p.id) || 0;
  const available = p.stock - inCart;
  const out = p.stock === 0;

  const meta = [p.platform, p.year || null, p.developer].filter(Boolean).map(esc).join(' · ');

  $('product-body').innerHTML = `
    <div class="product__art">
      <img src="${esc(p.art)}" alt="Обложка: ${esc(p.title)}" width="640" height="800">
      ${p.genre ? `<span class="card__genre">${esc(p.genre)}</span>` : ''}
    </div>
    <div class="product__info">
      <h1 class="product__title">${esc(p.title)}</h1>
      <div class="product__meta">${meta}</div>

      <a class="product__rating" href="#product-reviews-title">
        <span class="card__stars" aria-hidden="true">${stars(Math.round(rating.average))}</span>
        ${rating.total
          ? `<strong>${String(rating.average).replace('.', ',')}</strong>
             <span>${rating.total} ${plural(rating.total, 'отзыв', 'отзыва', 'отзывов')}</span>`
          : '<span>пока нет отзывов</span>'}
      </a>

      <p class="product__desc">${esc(p.description)}</p>

      <div class="product__buy">
        <div class="product__price">${money(p.price_kopecks)}</div>
        <div class="product__stock ${out ? 'is-out' : ''}">
          ${out ? 'Нет в наличии' : `В наличии: ${available} шт.`}
        </div>
        <button class="btn btn--primary btn--lg btn--block" data-add="${p.id}"
                ${out || available <= 0 ? 'disabled' : ''}>
          ${out ? 'Нет в наличии' : (inCart ? 'Добавить ещё' : 'В корзину')}
        </button>
        <div class="product__perks">
          <span>🚚 Доставка 1–3 дня</span>
          <span>🛡️ Гарантия 24 месяца</span>
          <span>↩️ Возврат 14 дней</span>
        </div>
      </div>
    </div>`;

  // ---------- характеристики ----------
  const rows = Object.entries(specs);
  $('product-specs').innerHTML = rows.length
    ? rows.map(([key, value]) => `
        <tr><th>${esc(key)}</th><td>${esc(value)}</td></tr>
      `).join('')
    : '<tr><td class="muted">Характеристики не заполнены</td></tr>';

  // ---------- отзывы ----------
  $('product-reviews-title').textContent =
    rating.total ? `Отзывы (${rating.total})` : 'Отзывы';
  renderRatingBlock($('product-rating'), rating);
  $('product-reviews').innerHTML = reviews.length
    ? reviews.map(reviewCard).join('')
    : '<p class="muted">Об этом товаре ещё никто не написал.</p>';

  renderReviewCta(state.product.can_review, state.product.already_reviewed);

  // ---------- рекомендации ----------
  $('product-recommendations').innerHTML = recommendations.map((item, index) => {
    const isOut = item.stock === 0;
    return `
      <article class="card ${isOut ? 'is-out' : ''}" data-card="${item.id}"
               style="animation-delay:${index * 40}ms">
        <a class="card__link" href="/product/${esc(item.sku)}"
           data-product="${esc(item.sku)}" aria-label="${esc(item.title)}"></a>
        <div class="card__art">
          <img class="card__cover" src="${esc(item.art)}" alt="" width="640" height="800"
               loading="lazy">
          ${item.genre ? `<span class="card__genre">${esc(item.genre)}</span>` : ''}
        </div>
        <div class="card__body">
          <h3 class="card__title">${esc(item.title)}</h3>
          <div class="card__meta">${esc(item.platform || item.category)}</div>
          <div class="card__foot">
            <div class="card__price">${money(item.price_kopecks)}</div>
            <button class="btn btn--primary btn--sm" data-add="${item.id}"
                    ${isOut ? 'disabled' : ''}>${isOut ? 'Нет' : 'В корзину'}</button>
          </div>
        </div>
      </article>`;
  }).join('');

  $('product-tabs').hidden = false;
}

/** Блок «оставить отзыв» на странице товара.
 *
 *  Форма показывается только тому, кто товар купил. Это удобство,
 *  а не защита: решение принимает сервер, и POST без покупки вернёт
 *  403 независимо от того, что показано на экране. */
function renderReviewCta(canReview, alreadyReviewed) {
  const box = $('review-cta');

  if (!state.customer) {
    box.innerHTML = `
      <div class="notice">
        <span aria-hidden="true">👤</span>
        <div>
          <strong>Отзыв можно оставить после покупки.</strong>
          Войдите в профиль — и, если товар куплен, здесь появится форма.
          <button class="link-btn" id="cta-pick" type="button">Войти или зарегистрироваться</button>
        </div>
      </div>`;
    return;
  }

  if (alreadyReviewed) {
    box.innerHTML = `
      <div class="notice notice--ok">
        <span aria-hidden="true">✓</span>
        <div>
          <strong>Вы уже оценили этот товар.</strong>
          Ваш отзыв в списке ниже. Один покупатель — один отзыв
          на товар, и это ограничение стоит в базе, а не только здесь.
        </div>
      </div>`;
    return;
  }

  if (!canReview) {
    box.innerHTML = `
      <div class="notice">
        <span aria-hidden="true">🛒</span>
        <div>
          <strong>Этого товара нет в ваших покупках.</strong>
          Отзыв о товаре может оставить только тот, кто его купил —
          проверку делает сервер.
        </div>
      </div>`;
    return;
  }

  box.innerHTML = `
    <form class="form form--compact" id="product-review-form">
      <div class="form__head">
        <span class="tag tag--ok">Покупка подтверждена</span>
        <span class="muted">вы покупали этот товар</span>
      </div>
      <div class="form__row">
        <label class="field">
          <span class="field__label">Оценка</span>
          <select class="field__input" name="rating">
            <option value="5">5 — отлично</option>
            <option value="4">4 — хорошо</option>
            <option value="3">3 — нормально</option>
            <option value="2">2 — так себе</option>
            <option value="1">1 — плохо</option>
          </select>
        </label>
        <label class="field">
          <span class="field__label">Заголовок <span class="field__opt">необязательно</span></span>
          <input class="field__input" name="title" maxlength="120" placeholder="Коротко о главном">
        </label>
      </div>
      <label class="field">
        <span class="field__label">Отзыв</span>
        <textarea class="field__input field__input--area" name="body" rows="4"
                  maxlength="4000" required placeholder="Что понравилось, что нет"></textarea>
      </label>
      <button class="btn btn--primary" type="submit">Отправить отзыв</button>
    </form>`;
}

async function submitProductReview(event) {
  event.preventDefault();
  if (!state.product || !state.customer) return;

  const form = event.target;
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  button.textContent = 'Отправляем…';

  try {
    await api('/reviews', {
      method: 'POST',
      body: JSON.stringify({
        author: state.customer.name,
        rating: Number(form.rating.value),
        title: form.title.value.trim(),
        body: form.body.value.trim(),
        product_id: state.product.product.id,
      }),
    });
    toast('Спасибо! Отзыв опубликован', 'success');
    await loadProduct(state.productSku);
    $('product-reviews-title').scrollIntoView({ behavior: 'smooth' });
  } catch (error) {
    toast(error.message, 'error');
    button.disabled = false;
    button.textContent = 'Отправить отзыв';
  }
}

/* ---------------------------------------------- отзывы */

function stars(rating) {
  return '★★★★★'.slice(0, rating) + '☆☆☆☆☆'.slice(0, 5 - rating);
}

async function loadReviews() {
  try {
    const data = await api('/reviews?limit=60');
    state.reviews = data.items;
    state.reviewSummary = data.summary;
    renderReviews();
  } catch (error) {
    $('reviews-list').innerHTML =
      `<p class="muted">Не удалось загрузить отзывы: ${esc(error.message)}</p>`;
  }
}

/** Одна карточка отзыва. Разметка общая для страницы отзывов
 *  и для страницы товара: дублировать её в двух местах значит
 *  однажды поправить только одно. */
function reviewCard(r) {
  return `
    <article class="review">
      <header class="review__head">
        <div class="review__who">
          <span class="review__avatar" aria-hidden="true">${esc(initials(r.author))}</span>
          <div>
            <div class="review__author">${esc(r.author)}</div>
            <div class="review__date">
              ${esc(r.created_at)}${r.city ? ' · ' + esc(r.city) : ''}
            </div>
          </div>
        </div>
        <div class="review__rating" aria-label="Оценка ${r.rating} из 5">
          ${stars(r.rating)}
        </div>
      </header>
      ${r.title ? `<h3 class="review__title">${esc(r.title)}</h3>` : ''}
      <p class="review__body">${esc(r.body)}</p>
      <footer class="review__foot">
        ${r.verified ? '<span class="tag tag--ok">Покупка подтверждена</span>' : ''}
        ${r.product_sku && state.page !== 'product'
          ? `<a class="review__product" href="/product/${esc(r.product_sku)}"
                data-product="${esc(r.product_sku)}">${esc(r.product_title)}</a>` : ''}
      </footer>
    </article>`;
}

/** Сводка по звёздам. Вынесена отдельно, потому что показывается
 *  и на общей странице отзывов, и на странице товара. */
function renderRatingBlock(box, summary) {
  // Ширина полосок считается от САМОЙ ЧАСТОЙ оценки, а не от общего
  // числа: иначе при 90% пятёрок остальные вырождаются в невидимые
  // чёрточки и график перестаёт что-либо показывать.
  const counts = [5, 4, 3, 2, 1].map((n) => (summary.stars || {})[n] || 0);
  const peak = Math.max(...counts, 1);

  box.innerHTML = `
    <div class="rating__score">
      <div class="rating__value">${summary.total
        ? summary.average.toFixed(1).replace('.', ',') : '—'}</div>
      <div class="rating__stars" aria-hidden="true">${stars(Math.round(summary.average))}</div>
      <div class="rating__count">${summary.total
        ? `${summary.total} ${plural(summary.total, 'отзыв', 'отзыва', 'отзывов')}`
        : 'пока нет отзывов'}</div>
    </div>
    <div class="rating__bars">
      ${[5, 4, 3, 2, 1].map((n, i) => `
        <div class="bar">
          <span class="bar__label">${n} ★</span>
          <span class="bar__track"><span class="bar__fill"
                style="width:${Math.round((counts[i] / peak) * 100)}%"></span></span>
          <span class="bar__count">${counts[i]}</span>
        </div>`).join('')}
    </div>`;
}

function renderReviews() {
  const summary = state.reviewSummary || { total: 0, average: 0, stars: {} };
  renderRatingBlock($('rating'), summary);

  $('reviews-list').innerHTML = state.reviews.length
    ? state.reviews.map(reviewCard).join('')
    : '<p class="muted">Отзывов пока нет — ваш будет первым.</p>';
}

function initials(name) {
  return name.trim().split(/\s+/).slice(0, 2).map((part) => part[0] || '').join('').toUpperCase();
}

async function submitReview(event) {
  event.preventDefault();
  const form = event.target;
  const button = $('review-submit');
  const payload = {
    author: form.author.value.trim(),
    rating: Number(form.rating.value),
    body: form.body.value.trim(),
    title: form.title.value.trim(),
  };

  if (!payload.author || !payload.body) {
    toast('Заполните имя и текст отзыва', 'error');
    return;
  }

  button.disabled = true;
  button.textContent = 'Отправляем…';
  try {
    await api('/reviews', { method: 'POST', body: JSON.stringify(payload) });
    form.reset();
    toast('Спасибо! Отзыв сохранён в базе', 'success');
    await loadReviews();
    $('reviews-list').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    button.disabled = false;
    button.textContent = 'Отправить отзыв';
  }
}


/* ---------------------------------------------- профиль

   Настоящего входа в проекте нет. Делать форму с паролем, который
   ничего не проверяет, — обман того, кто смотрит проект: выглядит
   как аутентификация, а ею не является.

   Поэтому здесь честный механизм: список профилей из базы, выбранный
   сохраняется в браузере и уходит заголовком. Серверу всё равно,
   откуда взялся идентификатор, — в настоящем сервисе на этом месте
   будет разбор токена, и остальной код не изменится. */

const TOKEN_KEY = 'gameport-token';
const KNOWN_KEY = 'gameport-known-accounts';

/** Восстанавливает сессию из браузера.
 *
 *  В хранилище лежит только токен: профиль подтверждается запросом
 *  к серверу. Держать имя и адрес в localStorage и верить им было бы
 *  ошибкой — их правит кто угодно через консоль, а показывать чужое
 *  имя вошедшему нельзя. */
function loadSavedSession() {
  try {
    state.token = localStorage.getItem(TOKEN_KEY);
  } catch { /* приватный режим */ }
  updateProfileBadge();
}

function saveSession(token, profile) {
  state.token = token;
  state.customer = profile;
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch { /* не критично: сессия не запомнится */ }

  // Список адресов, под которыми уже входили с этого браузера.
  // Нужен только для подстановки в форму — паролей здесь нет
  // и быть не может.
  if (profile) rememberAccount(profile);
  updateProfileBadge();
}

function knownAccounts() {
  try {
    return JSON.parse(localStorage.getItem(KNOWN_KEY) || '[]');
  } catch { return []; }
}

function rememberAccount(profile) {
  const list = knownAccounts().filter((a) => a.email !== profile.email);
  list.unshift({ email: profile.email, name: profile.name });
  try {
    localStorage.setItem(KNOWN_KEY, JSON.stringify(list.slice(0, 5)));
  } catch { /* не критично */ }
}

function forgetAccount(email) {
  try {
    localStorage.setItem(KNOWN_KEY,
      JSON.stringify(knownAccounts().filter((a) => a.email !== email)));
  } catch { /* не критично */ }
}

function updateProfileBadge() {
  $('profile-dot').hidden = !state.customer;
  $('profile-open').setAttribute(
    'aria-label', state.customer ? `Профиль: ${state.customer.name}` : 'Войти'
  );
}

/** Спрашивает сервер, кто вошёл.
 *
 *  Вызывается при загрузке страницы: токен мог истечь или быть
 *  погашен выходом на другом устройстве, и доверять локальной копии
 *  профиля нельзя. */
async function restoreSession() {
  if (!state.token) return;
  try {
    const data = await api('/auth/me');
    state.customer = data.profile;
    if (!data.profile) saveSession(null, null);
  } catch {
    saveSession(null, null);
  }
  updateProfileBadge();
}

/* ---------------------------------------------- вход и регистрация */

function renderAuth(mode = 'login') {
  const box = $('profile-body');
  const accounts = knownAccounts();

  const tabs = `
    <div class="auth__tabs" role="tablist">
      <button class="auth__tab ${mode === 'login' ? 'is-active' : ''}"
              type="button" data-auth-tab="login" role="tab">Вход</button>
      <button class="auth__tab ${mode === 'register' ? 'is-active' : ''}"
              type="button" data-auth-tab="register" role="tab">Регистрация</button>
    </div>`;

  const remembered = (mode === 'login' && accounts.length) ? `
    <div class="auth__known">
      <div class="auth__known-title">Вы уже входили с этого браузера</div>
      ${accounts.map((a) => `
        <div class="auth__known-item">
          <button class="auth__known-pick" type="button" data-fill-email="${esc(a.email)}">
            <span class="auth__known-avatar">${esc(initials(a.name))}</span>
            <span>
              <span class="auth__known-name">${esc(a.name)}</span>
              <span class="auth__known-email">${esc(a.email)}</span>
            </span>
          </button>
          <button class="auth__known-forget" type="button" data-forget-email="${esc(a.email)}"
                  aria-label="Убрать из списка">&times;</button>
        </div>`).join('')}
      <p class="auth__known-note">
        Список хранится только в этом браузере. Паролей в нём нет —
        только адрес для подстановки в форму.
      </p>
    </div>` : '';

  box.innerHTML = `
    <div class="auth">
      ${tabs}
      ${remembered}
      <form class="form" id="auth-form" data-mode="${mode}" novalidate>
        ${mode === 'register' ? `
          <div class="form__row">
            <label class="field">
              <span class="field__label">Имя</span>
              <input class="field__input" name="name" maxlength="80" required
                     autocomplete="name" placeholder="Как вас зовут">
            </label>
            <label class="field">
              <span class="field__label">Город <span class="field__opt">необязательно</span></span>
              <input class="field__input" name="city" maxlength="80"
                     autocomplete="address-level2" placeholder="Москва">
            </label>
          </div>` : ''}

        <label class="field">
          <span class="field__label">Почта</span>
          <input class="field__input" name="email" type="email" required
                 autocomplete="email" placeholder="you@example.com">
        </label>

        <label class="field">
          <span class="field__label">Пароль</span>
          <input class="field__input" name="password" type="password" required
                 autocomplete="${mode === 'register' ? 'new-password' : 'current-password'}"
                 minlength="8" placeholder="${mode === 'register' ? 'минимум 8 символов' : ''}">
          ${mode === 'register' ? `
            <span class="field__hint">
              Длина важнее состава: длинная фраза надёжнее, чем «Пароль1!».
            </span>` : ''}
        </label>

        <button class="btn btn--primary btn--lg btn--block" type="submit">
          ${mode === 'register' ? 'Зарегистрироваться' : 'Войти'}
        </button>
      </form>

      <p class="auth__note">
        Пароль хранится не как текст, а как результат scrypt со своей солью.
        Сессия живёт токеном 30 дней, и в базе лежит его хеш.
      </p>
    </div>`;
}

async function submitAuth(event) {
  event.preventDefault();
  const form = event.target;
  const mode = form.dataset.mode;
  const button = form.querySelector('button[type="submit"]');

  const body = {
    email: form.email.value.trim(),
    password: form.password.value,
  };
  if (mode === 'register') {
    body.name = form.name.value.trim();
    body.city = form.city.value.trim();
    if (!body.name) { toast('Заполните имя', 'error'); return; }
    if (body.password.length < 8) {
      toast('Пароль короче 8 символов', 'error'); return;
    }
  }
  if (!body.email || !body.password) {
    toast('Заполните почту и пароль', 'error'); return;
  }

  button.disabled = true;
  button.textContent = mode === 'register' ? 'Создаём…' : 'Входим…';
  try {
    const data = await api(`/auth/${mode}`, {
      method: 'POST', body: JSON.stringify(body),
    });
    saveSession(data.token, data.profile);
    toast(mode === 'register'
      ? `Добро пожаловать, ${data.profile.name}!`
      : `Вы вошли как ${data.profile.name}`, 'success');
    // Страница товара знает про право на отзыв — её надо перечитать.
    state.product = null;
    loadProfile();
  } catch (error) {
    toast(error.message, 'error');
    button.disabled = false;
    button.textContent = mode === 'register' ? 'Зарегистрироваться' : 'Войти';
  }
}

async function logout() {
  try {
    await api('/auth/logout', { method: 'POST' });
  } catch { /* сессия могла уже истечь — выходим в любом случае */ }
  saveSession(null, null);
  state.me = null;
  state.product = null;
  toast('Вы вышли из профиля');
  renderAuth('login');
}

async function loadProfile() {
  if (!state.token) { renderAuth('login'); return; }

  const box = $('profile-body');
  box.innerHTML = '<p class="muted">Загружаем профиль…</p>';

  try {
    const data = await api('/customers/me');
    state.me = data;
    renderProfile();
  } catch (error) {
    if (error.status === 401) { saveSession(null, null); renderAuth('login'); return; }
    box.innerHTML = `<p class="muted">Не удалось загрузить профиль: ${esc(error.message)}</p>`;
  }
}

function renderProfile() {
  const { profile, orders, purchased } = state.me;
  const toReview = purchased.filter((item) => !item.reviewed);

  $('profile-body').innerHTML = `
    <div class="profile__head">
      <span class="profile__avatar">${esc(initials(profile.name))}</span>
      <div>
        <h2 class="profile__name">${esc(profile.name)}</h2>
        <div class="profile__meta">
          ${esc(profile.email)}${profile.city ? ' · ' + esc(profile.city) : ''}
        </div>
        <div class="profile__since">С нами с ${esc(profile.joined_at)}</div>
      </div>
      <button class="btn btn--ghost" id="logout" type="button">Выйти</button>
    </div>

    <div class="numbers numbers--compact">
      <div class="number"><strong>${orders.length}</strong><span>заказов</span></div>
      <div class="number"><strong>${purchased.length}</strong><span>товаров куплено</span></div>
      <div class="number"><strong>${toReview.length}</strong><span>ждут отзыва</span></div>
    </div>

    ${toReview.length ? `
      <h2 class="section-title">Можно оставить отзыв</h2>
      <p class="section-sub">Эти товары вы купили, но ещё не оценили.</p>
      <div class="to-review">
        ${toReview.map((item) => `
          <a class="to-review__item" href="/product/${esc(item.sku)}"
             data-product="${esc(item.sku)}">
            <img src="${esc(item.art)}" alt="" width="640" height="800" loading="lazy">
            <span class="to-review__title">${esc(item.title)}</span>
            <span class="to-review__hint">Оставить отзыв →</span>
          </a>`).join('')}
      </div>` : ''}

    <h2 class="section-title">Заказы</h2>
    ${orders.length ? `
      <div class="orders__list">
        ${orders.map((order) => `
          <div class="order-row">
            <div>
              <div class="order-row__id">Заказ №${order.id}</div>
              <div class="order-row__meta">
                ${order.lines.length} ${plural(order.lines.length, 'позиция', 'позиции', 'позиций')}
                · ${esc(order.created_at)}
              </div>
            </div>
            <div style="display:flex;align-items:center;gap:16px">
              <span class="status status--${esc(order.status)}">${statusLabel(order.status)}</span>
              <span class="order-row__sum">${money(order.total_kopecks)}</span>
            </div>
          </div>`).join('')}
      </div>`
      : `<p class="muted">Заказов пока нет. Купите что-нибудь — и сможете оставить отзыв.</p>`}

    ${purchased.length ? `
      <h2 class="section-title">Ваши покупки</h2>
      <div class="to-review">
        ${purchased.map((item) => `
          <a class="to-review__item ${item.reviewed ? 'is-done' : ''}"
             href="/product/${esc(item.sku)}" data-product="${esc(item.sku)}">
            <img src="${esc(item.art)}" alt="" width="640" height="800" loading="lazy">
            <span class="to-review__title">${esc(item.title)}</span>
            <span class="to-review__hint">
              ${item.reviewed ? '✓ отзыв оставлен' : 'без отзыва'}
            </span>
          </a>`).join('')}
      </div>` : ''}`;
}

/* ---------------------------------------------- тема */

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem('gameport-theme'); } catch { /* приватный режим */ }
  const system = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  document.documentElement.dataset.theme = saved || system;
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('gameport-theme', next); } catch { /* не критично */ }
}

/* ---------------------------------------------- события */

function bind() {
  document.querySelector('.logo').addEventListener('click', (event) => {
    event.preventDefault();
    showPage('catalog');
  });

  $('theme-toggle').addEventListener('click', toggleTheme);
  $('cart-open').addEventListener('click', openCart);
  $('cart-close').addEventListener('click', closeCart);
  $('backdrop').addEventListener('click', closeCart);
  $('cart-continue').addEventListener('click', closeCart);
  $('checkout').addEventListener('click', checkout);
  $('success-close').addEventListener('click', closeCart);
  $('success-cancel').addEventListener('click', cancelLastOrder);

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && $('cart').classList.contains('is-open')) closeCart();
  });

  // Делегирование вместо обработчика на каждой кнопке: карточки
  // перерисовываются, и вешать слушатели заново пришлось бы каждый раз.
  document.addEventListener('click', (event) => {
    const add = event.target.closest('[data-add]');
    if (add) { addToCart(Number(add.dataset.add)); return; }

    const inc = event.target.closest('[data-inc]');
    if (inc) { changeQty(Number(inc.dataset.inc), 1); return; }

    const dec = event.target.closest('[data-dec]');
    if (dec) { changeQty(Number(dec.dataset.dec), -1); return; }

    const chip = event.target.closest('[data-category]');
    if (chip) {
      // Ссылка «Консоли» в шапке ведёт и на каталог, и в категорию:
      // сначала переключаем раздел, потом фильтр.
      if (state.page !== 'catalog') showPage('catalog', { scroll: false });
      state.category = chip.dataset.category;
      loadCatalog();
      if (chip.tagName === 'A') $('catalog').scrollIntoView({ behavior: 'smooth' });
      return;
    }

    const platform = event.target.closest('[data-platform]');
    if (platform) {
      state.platform = platform.dataset.platform;
      loadCatalog();
      return;
    }

    // Переход на товар. Проверяется ДО навигации: у ссылки на карточке
    // есть и data-product, и обычный href, и порядок здесь важен.
    const productLink = event.target.closest('[data-product]');
    if (productLink && isPlainClick(event)) {
      event.preventDefault();
      showPage('product', { sku: productLink.dataset.product });
      return;
    }

    // Навигация перехватывается здесь же. Клики с модификаторами
    // не трогаем: Ctrl+клик и «открыть в новой вкладке» должны
    // работать как всегда, иначе роутер ломает привычное поведение
    // браузера — и это раздражает сильнее, чем отсутствие роутера.
    const link = event.target.closest('[data-nav]');
    if (link && isPlainClick(event)) {
      event.preventDefault();
      showPage(link.dataset.nav);
      return;
    }

    // Переключение между вкладками входа и регистрации.
    const tab = event.target.closest('[data-auth-tab]');
    if (tab) { renderAuth(tab.dataset.authTab); return; }

    // Подстановка адреса из списка «уже входили».
    const fill = event.target.closest('[data-fill-email]');
    if (fill) {
      const input = document.querySelector('#auth-form input[name="email"]');
      if (input) {
        input.value = fill.dataset.fillEmail;
        document.querySelector('#auth-form input[name="password"]').focus();
      }
      return;
    }

    const forget = event.target.closest('[data-forget-email]');
    if (forget) { forgetAccount(forget.dataset.forgetEmail); renderAuth('login'); return; }

    if (event.target.closest('#logout')) { logout(); return; }
    if (event.target.closest('#pick-customer')) { renderAuth('login'); return; }
    if (event.target.closest('#cta-pick')) {
      showPage('profile');
      renderAuth('login');
      return;
    }
  });

  // Форма отзыва на странице товара создаётся заново при каждой
  // отрисовке, поэтому обработчик вешается делегированием, а не
  // на конкретный элемент: иначе после перерисовки он теряется.
  // Формы создаются заново при каждой отрисовке, поэтому обработчик
  // вешается делегированием: иначе после перерисовки он теряется.
  document.addEventListener('submit', (event) => {
    if (event.target.id === 'product-review-form') submitProductReview(event);
    if (event.target.id === 'auth-form') submitAuth(event);
  });

  $('profile-open').addEventListener('click', () => showPage('profile'));

  addEventListener('popstate', () => {
    // Кнопки «назад» и «вперёд». push:false — адрес уже изменил
    // браузер, и повторный pushState положил бы в историю дубль.
    const route = routeFromPath(location.pathname);
    showPage(route.page, { push: false, sku: route.sku });
  });

  $('review-form').addEventListener('submit', submitReview);

  const onSearch = debounce(() => {
    state.search = $('search').value;
    $('search-clear').hidden = !state.search;
    loadCatalog();
  }, 260);
  $('search').addEventListener('input', onSearch);

  $('search-clear').addEventListener('click', () => {
    $('search').value = '';
    state.search = '';
    $('search-clear').hidden = true;
    loadCatalog();
  });

  $('sort').addEventListener('change', (event) => {
    state.sort = event.target.value;
    renderGrid();
  });

  $('reset-filters').addEventListener('click', () => {
    state.category = 'all';
    state.platform = 'all';
    state.search = '';
    $('search').value = '';
    $('search-clear').hidden = true;
    loadCatalog();
  });

  // Тень у шапки появляется только когда под ней есть содержимое.
  const header = $('header');
  addEventListener('scroll', () => {
    header.classList.toggle('is-stuck', scrollY > 8);
  }, { passive: true });
}

/* ---------------------------------------------- старт */

initTheme();
loadSavedSession();
bind();
// Раздел берётся из адреса, а не всегда каталог: страница могла быть
// открыта по прямой ссылке или перезагружена на /reviews.
const startRoute = routeFromPath(location.pathname);
showPage(startRoute.page, { push: false, scroll: false, sku: startRoute.sku });
restoreSession();
loadCatalog();
loadOrders();
renderCart();
