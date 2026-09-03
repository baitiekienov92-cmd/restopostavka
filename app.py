import os
import base64
import json
import requests
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import quote

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, Response
from werkzeug.utils import secure_filename
from models import (
    db, User, Restaurant, Product, Category, RestaurantPrice, Order, OrderItem, ORDER_STATUSES,
    Supplier, SupplierRun, SupplierRunItem, SupplierInvoicePhoto, RUN_STATUSES,
    OrderDisputePhoto, RestaurantPayment, SupplierPayment,
    AdminNotification, PaymentNotice,
)

try:
    from webauthn import (
        generate_registration_options, verify_registration_response,
        generate_authentication_options, verify_authentication_response,
        options_to_json,
    )
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria, UserVerificationRequirement,
        PublicKeyCredentialDescriptor, RegistrationCredential, AuthenticationCredential,
    )
    from webauthn.helpers import bytes_to_base64url, base64url_to_bytes
    WEBAUTHN_AVAILABLE = True
except ImportError:
    WEBAUTHN_AVAILABLE = False

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'restopostavka.db')}"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = os.path.join(BASE_DIR, "static", "uploads", "invoices")
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
app.config["DISPUTE_UPLOAD_FOLDER"] = os.path.join(BASE_DIR, "static", "uploads", "disputes")
os.makedirs(app.config["DISPUTE_UPLOAD_FOLDER"], exist_ok=True)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = "claude-sonnet-5"

db.init_app(app)

# Заказы на сегодня принимаются с дедлайном (час), после — уходят на послезавтра
ORDER_CUTOFF_HOUR = 18


# ---------- Вспомогательные функции ----------

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user or not user.is_admin:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def next_delivery_date():
    now = datetime.now()
    d = date.today() + timedelta(days=1)
    if now.hour >= ORDER_CUTOFF_HOUR:
        d = d + timedelta(days=1)
    return d


def rp_id():
    return request.host.split(":")[0]


def rp_origin():
    return request.url_root.rstrip("/")


def notify_admin(kind, message):
    db.session.add(AdminNotification(kind=kind, message=message))
    db.session.commit()


@app.context_processor
def inject_globals():
    unread_count = 0
    user = current_user()
    if user and user.is_admin:
        unread_count = AdminNotification.query.filter_by(is_read=False).count()
        unread_count += PaymentNotice.query.filter_by(status="pending").count()
    return {"current_user": user, "now": datetime.now(), "unread_notifications": unread_count, "webauthn_available": WEBAUTHN_AVAILABLE}


# ---------- Авторизация ----------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(phone=phone).first()
        if user and user.check_password(password):
            if not user.is_admin and user.restaurant and not user.restaurant.is_active:
                flash("Ваша заявка на регистрацию ещё не одобрена админом. Ожидайте подтверждения.", "error")
                return render_template("login.html")
            session["user_id"] = user.id
            flash(f"Добро пожаловать, {user.name}!", "success")
            if user.is_admin:
                return redirect(url_for("admin_orders"))
            return redirect(url_for("catalog"))
        flash("Неверный телефон или пароль", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        restaurant_name = request.form.get("restaurant_name", "").strip()
        address = request.form.get("address", "").strip()
        contact_name = request.form.get("contact_name", "").strip()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")

        if not all([restaurant_name, contact_name, phone, password]):
            flash("Заполни все обязательные поля", "error")
            return render_template("register.html")

        if User.query.filter_by(phone=phone).first():
            flash("Пользователь с таким телефоном уже зарегистрирован", "error")
            return render_template("register.html")

        restaurant = Restaurant(name=restaurant_name, address=address, phone=phone, is_active=False)
        db.session.add(restaurant)
        db.session.flush()

        user = User(name=contact_name, phone=phone, role="restaurant", restaurant_id=restaurant.id)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        notify_admin(
            "registration",
            f"🆕 Новая заявка на регистрацию: ресторан «{restaurant_name}» ({contact_name}, {phone}). "
            f"Одобри в разделе «Рестораны»."
        )
        flash("Заявка отправлена! Как только админ одобрит — сможешь войти.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- Face ID / Touch ID (WebAuthn) ----------

@app.route("/webauthn/register/begin", methods=["POST"])
@login_required
def webauthn_register_begin():
    if not WEBAUTHN_AVAILABLE:
        return jsonify({"error": "unavailable"}), 400
    user = current_user()
    options = generate_registration_options(
        rp_id=rp_id(),
        rp_name="restopostavka",
        user_id=str(user.id).encode(),
        user_name=user.phone,
        user_display_name=user.name,
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    session["webauthn_challenge"] = bytes_to_base64url(options.challenge)
    return Response(options_to_json(options), mimetype="application/json")


@app.route("/webauthn/register/complete", methods=["POST"])
@login_required
def webauthn_register_complete():
    if not WEBAUTHN_AVAILABLE:
        return jsonify({"error": "unavailable"}), 400
    user = current_user()
    challenge = base64url_to_bytes(session.pop("webauthn_challenge", ""))
    try:
        credential = RegistrationCredential.parse_raw(request.data)
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_origin=rp_origin(),
            expected_rp_id=rp_id(),
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    user.webauthn_credential_id = bytes_to_base64url(verification.credential_id)
    user.webauthn_public_key = bytes_to_base64url(verification.credential_public_key)
    user.webauthn_sign_count = verification.sign_count
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/webauthn/login/begin", methods=["POST"])
def webauthn_login_begin():
    if not WEBAUTHN_AVAILABLE:
        return jsonify({"error": "unavailable"}), 400
    phone = request.form.get("phone", "").strip()
    user = User.query.filter_by(phone=phone).first()
    if not user or not user.has_faceid:
        return jsonify({"error": "no_credential"}), 400

    options = generate_authentication_options(
        rp_id=rp_id(),
        allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(user.webauthn_credential_id))],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    session["webauthn_challenge"] = bytes_to_base64url(options.challenge)
    session["webauthn_login_user_id"] = user.id
    return Response(options_to_json(options), mimetype="application/json")


@app.route("/webauthn/login/complete", methods=["POST"])
def webauthn_login_complete():
    if not WEBAUTHN_AVAILABLE:
        return jsonify({"error": "unavailable"}), 400
    user_id = session.pop("webauthn_login_user_id", None)
    challenge = base64url_to_bytes(session.pop("webauthn_challenge", ""))
    user = User.query.get(user_id) if user_id else None
    if not user:
        return jsonify({"error": "session_expired"}), 400

    try:
        credential = AuthenticationCredential.parse_raw(request.data)
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_origin=rp_origin(),
            expected_rp_id=rp_id(),
            credential_public_key=base64url_to_bytes(user.webauthn_public_key),
            credential_current_sign_count=user.webauthn_sign_count,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    user.webauthn_sign_count = verification.new_sign_count
    db.session.commit()
    session["user_id"] = user.id
    redirect_url = url_for("admin_orders") if user.is_admin else url_for("catalog")
    return jsonify({"ok": True, "redirect": redirect_url})


# ---------- Ресторан: каталог и заказ ----------

@app.route("/")
@login_required
def catalog():
    user = current_user()
    if user.is_admin:
        return redirect(url_for("admin_orders"))

    categories = Category.query.order_by(Category.sort_order).all()
    products = Product.query.filter_by(is_active=True).all()
    prices = {p.id: user.restaurant.price_for(p) for p in products}
    cart = session.get("cart", {})
    delivery_date = next_delivery_date()

    return render_template(
        "catalog.html",
        categories=categories,
        products=products,
        prices=prices,
        cart=cart,
        delivery_date=delivery_date,
    )


@app.route("/cart/update", methods=["POST"])
@login_required
def cart_update():
    product_id = request.form.get("product_id")
    qty = request.form.get("qty", "0")
    try:
        qty = float(qty)
    except ValueError:
        qty = 0

    cart = session.get("cart", {})
    if qty > 0:
        cart[product_id] = qty
    else:
        cart.pop(product_id, None)
    session["cart"] = cart
    session.modified = True

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True, "cart_count": len(cart)})
    return redirect(url_for("catalog"))


@app.route("/order/checkout", methods=["GET", "POST"])
@login_required
def checkout():
    user = current_user()
    cart = session.get("cart", {})
    if not cart:
        flash("Корзина пуста", "error")
        return redirect(url_for("catalog"))

    products = {p.id: p for p in Product.query.filter(Product.id.in_([int(k) for k in cart.keys()])).all()}

    if request.method == "POST":
        comment = request.form.get("comment", "")
        order = Order(
            restaurant_id=user.restaurant_id,
            created_by_id=user.id,
            delivery_date=next_delivery_date(),
            status="new",
            comment=comment,
        )
        db.session.add(order)
        db.session.flush()

        for pid_str, qty in cart.items():
            product = products.get(int(pid_str))
            if not product:
                continue
            price = user.restaurant.price_for(product)
            db.session.add(OrderItem(order_id=order.id, product_id=product.id, qty=qty, price=price))

        db.session.commit()
        session["cart"] = {}
        flash(f"Заказ №{order.id} оформлен на {order.delivery_date.strftime('%d.%m.%Y')}", "success")
        return redirect(url_for("order_history"))

    items = []
    total = 0
    for pid_str, qty in cart.items():
        product = products.get(int(pid_str))
        if not product:
            continue
        price = user.restaurant.price_for(product)
        subtotal = price * qty
        total += subtotal
        items.append({"product": product, "qty": qty, "price": price, "subtotal": subtotal})

    return render_template(
        "checkout.html", items=items, total=total, delivery_date=next_delivery_date()
    )


@app.route("/orders")
@login_required
def order_history():
    user = current_user()
    orders = (
        Order.query.filter_by(restaurant_id=user.restaurant_id)
        .order_by(Order.created_at.desc())
        .limit(50)
        .all()
    )
    return render_template("order_history.html", orders=orders)


@app.route("/orders/<int:order_id>/repeat")
@login_required
def order_repeat(order_id):
    user = current_user()
    order = Order.query.filter_by(id=order_id, restaurant_id=user.restaurant_id).first_or_404()
    cart = {str(item.product_id): item.qty for item in order.items if item.product.is_active}
    session["cart"] = cart
    session.modified = True
    flash("Товары из заказа добавлены в корзину", "success")
    return redirect(url_for("catalog"))


@app.route("/orders/<int:order_id>/confirm", methods=["POST"])
@login_required
def order_confirm(order_id):
    user = current_user()
    order = Order.query.filter_by(id=order_id, restaurant_id=user.restaurant_id).first_or_404()
    if order.status == "delivered":
        order.restaurant_confirmed_at = datetime.utcnow()
        order.dispute_flag = False
        db.session.commit()
        notify_admin(
            "confirmation",
            f"✅ Ресторан «{order.restaurant.name}» подтвердил приёмку заказа №{order.id} на {order.total:.0f} ₸"
        )
        flash(f"Заказ №{order.id} подтверждён", "success")
    return redirect(url_for("order_history"))


@app.route("/orders/<int:order_id>/dispute", methods=["POST"])
@login_required
def order_dispute(order_id):
    user = current_user()
    order = Order.query.filter_by(id=order_id, restaurant_id=user.restaurant_id).first_or_404()
    order.dispute_flag = True
    order.dispute_note = request.form.get("dispute_note", "")
    order.restaurant_confirmed_at = None
    db.session.commit()

    photo = request.files.get("dispute_photo")
    if photo and photo.filename:
        filename = secure_filename(f"order{order_id}_{datetime.utcnow().timestamp()}_{photo.filename}")
        filepath = os.path.join(app.config["DISPUTE_UPLOAD_FOLDER"], filename)
        photo.save(filepath)
        db.session.add(OrderDisputePhoto(order_id=order.id, filepath=f"uploads/disputes/{filename}"))
        db.session.commit()

    notify_admin(
        "dispute",
        f"⚠️ СПОР по заказу №{order.id} от «{order.restaurant.name}»: {order.dispute_note or 'без комментария'}"
    )
    flash(f"По заказу №{order.id} отправлено уведомление о расхождении", "success")
    return redirect(url_for("order_history"))


@app.route("/orders/payment_notice", methods=["POST"])
@login_required
def order_payment_notice():
    user = current_user()
    amount = float(request.form.get("amount", 0) or 0)
    note = request.form.get("note", "")
    if amount > 0:
        db.session.add(PaymentNotice(restaurant_id=user.restaurant_id, amount=amount, note=note))
        db.session.commit()
        notify_admin(
            "payment_notice",
            f"💰 Ресторан «{user.restaurant.name}» сообщает об оплате {amount:.0f} ₸" + (f" ({note})" if note else "")
        )
        flash("Спасибо! Сообщили админу об оплате.", "success")
    return redirect(url_for("order_history"))


# ---------- Админка ----------

@app.route("/admin/orders")
@admin_required
def admin_orders():
    date_filter = request.args.get("date")
    q = Order.query
    if date_filter:
        try:
            d = datetime.strptime(date_filter, "%Y-%m-%d").date()
            q = q.filter(Order.delivery_date == d)
        except ValueError:
            pass
    else:
        d = next_delivery_date()
        q = q.filter(Order.delivery_date == d)
        date_filter = d.strftime("%Y-%m-%d")

    orders = q.order_by(Order.created_at.desc()).all()
    return render_template("admin_orders.html", orders=orders, date_filter=date_filter, statuses=ORDER_STATUSES)


@app.route("/admin/orders/<int:order_id>/status", methods=["POST"])
@admin_required
def admin_order_status(order_id):
    order = Order.query.get_or_404(order_id)
    status = request.form.get("status")
    if status in ORDER_STATUSES:
        order.status = status
        if status == "delivered" and not order.delivered_at:
            order.delivered_at = datetime.utcnow()
        db.session.commit()
        flash(f"Статус заказа №{order.id} обновлён", "success")
    return redirect(request.referrer or url_for("admin_orders"))


@app.route("/admin/orders/<int:order_id>/deliver", methods=["POST"])
@admin_required
def admin_order_deliver(order_id):
    """Водитель фиксирует передачу заказа ресторану со своего телефона."""
    order = Order.query.get_or_404(order_id)
    order.status = "delivered"
    order.delivered_at = datetime.utcnow()
    order.delivered_note = request.form.get("delivered_note", "")
    db.session.commit()
    flash(f"Заказ №{order.id} отмечен как выданный", "success")
    return redirect(request.referrer or url_for("admin_orders"))


@app.route("/admin/orders/<int:order_id>/print")
@admin_required
def admin_order_print(order_id):
    order = Order.query.get_or_404(order_id)
    return render_template("order_invoice_print.html", order=order)


@app.route("/admin/summary")
@admin_required
def admin_summary():
    """Сводный лист закупа: сколько каждого товара нужно суммарно на дату."""
    date_filter = request.args.get("date")
    if date_filter:
        d = datetime.strptime(date_filter, "%Y-%m-%d").date()
    else:
        d = next_delivery_date()
        date_filter = d.strftime("%Y-%m-%d")

    orders = Order.query.filter(
        Order.delivery_date == d, Order.status != "cancelled"
    ).all()

    summary = {}
    for order in orders:
        for item in order.items:
            key = item.product_id
            if key not in summary:
                summary[key] = {"product": item.product, "qty": 0}
            summary[key]["qty"] += item.qty

    summary_list = sorted(summary.values(), key=lambda x: x["product"].name)
    return render_template("admin_summary.html", summary=summary_list, date_filter=date_filter, orders=orders)


@app.route("/admin/products")
@admin_required
def admin_products():
    products = Product.query.order_by(Product.category_id).all()
    categories = Category.query.order_by(Category.sort_order).all()
    return render_template("admin_products.html", products=products, categories=categories)


@app.route("/admin/products/add", methods=["POST"])
@admin_required
def admin_product_add():
    name = request.form.get("name")
    unit = request.form.get("unit", "кг")
    price = float(request.form.get("base_price", 0) or 0)
    category_id = request.form.get("category_id") or None
    product = Product(name=name, unit=unit, base_price=price, category_id=category_id)
    db.session.add(product)
    db.session.commit()
    flash(f"Товар «{name}» добавлен", "success")
    return redirect(url_for("admin_products"))


@app.route("/admin/products/<int:product_id>/update", methods=["POST"])
@admin_required
def admin_product_update(product_id):
    product = Product.query.get_or_404(product_id)
    product.base_price = float(request.form.get("base_price", product.base_price))
    product.is_active = request.form.get("is_active") == "on"
    db.session.commit()
    return redirect(url_for("admin_products"))


@app.route("/admin/restaurants")
@admin_required
def admin_restaurants():
    restaurants = Restaurant.query.order_by(Restaurant.is_active.asc(), Restaurant.name).all()
    return render_template("admin_restaurants.html", restaurants=restaurants)


@app.route("/admin/restaurants/<int:restaurant_id>/approve", methods=["POST"])
@admin_required
def admin_restaurant_approve(restaurant_id):
    restaurant = Restaurant.query.get_or_404(restaurant_id)
    restaurant.is_active = True
    db.session.commit()
    flash(f"Ресторан «{restaurant.name}» одобрен, может входить", "success")
    return redirect(url_for("admin_restaurants"))


@app.route("/admin/restaurants/add", methods=["POST"])
@admin_required
def admin_restaurant_add():
    name = request.form.get("name")
    address = request.form.get("address")
    phone = request.form.get("phone")
    r = Restaurant(name=name, address=address, phone=phone)
    db.session.add(r)
    db.session.commit()
    flash(f"Ресторан «{name}» добавлен", "success")
    return redirect(url_for("admin_restaurants"))


@app.route("/admin/restaurants/<int:restaurant_id>/add_user", methods=["POST"])
@admin_required
def admin_restaurant_add_user(restaurant_id):
    name = request.form.get("user_name")
    phone = request.form.get("user_phone")
    password = request.form.get("user_password")
    user = User(name=name, phone=phone, role="restaurant", restaurant_id=restaurant_id)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    flash(f"Пользователь {name} добавлен", "success")
    return redirect(url_for("admin_restaurants"))


# ---------- Поставщики и закупочные рейсы ----------

@app.route("/admin/suppliers")
@admin_required
def admin_suppliers():
    suppliers = Supplier.query.order_by(Supplier.name).all()
    return render_template("admin_suppliers.html", suppliers=suppliers)


@app.route("/admin/suppliers/add", methods=["POST"])
@admin_required
def admin_supplier_add():
    name = request.form.get("name")
    phone = request.form.get("whatsapp_phone", "").strip().replace("+", "").replace(" ", "")
    notes = request.form.get("notes")
    db.session.add(Supplier(name=name, whatsapp_phone=phone, notes=notes))
    db.session.commit()
    flash(f"Поставщик «{name}» добавлен", "success")
    return redirect(url_for("admin_suppliers"))


@app.route("/admin/procurement")
@admin_required
def admin_procurement():
    """Распределение сегодняшнего сводного закупа по поставщикам."""
    date_filter = request.args.get("date")
    if date_filter:
        d = datetime.strptime(date_filter, "%Y-%m-%d").date()
    else:
        d = next_delivery_date()
        date_filter = d.strftime("%Y-%m-%d")

    orders = Order.query.filter(Order.delivery_date == d, Order.status != "cancelled").all()

    needed = {}
    for order in orders:
        for item in order.items:
            needed.setdefault(item.product_id, {"product": item.product, "qty": 0})
            needed[item.product_id]["qty"] += item.qty

    already_assigned = {}
    runs = SupplierRun.query.filter_by(purchase_date=d).all()
    for run in runs:
        for ri in run.items:
            already_assigned[ri.product_id] = {"supplier": run.supplier, "qty": ri.qty, "run_id": run.id}

    rows = []
    for pid, data in sorted(needed.items(), key=lambda x: x[1]["product"].name):
        rows.append({
            "product": data["product"],
            "qty": data["qty"],
            "assigned": already_assigned.get(pid),
        })

    suppliers = Supplier.query.filter_by(is_active=True).order_by(Supplier.name).all()
    return render_template(
        "admin_procurement.html", rows=rows, suppliers=suppliers, date_filter=date_filter, runs=runs
    )


@app.route("/admin/procurement/assign", methods=["POST"])
@admin_required
def admin_procurement_assign():
    date_filter = request.form.get("date_filter")
    d = datetime.strptime(date_filter, "%Y-%m-%d").date()

    product_ids = request.form.getlist("product_id")
    supplier_ids = request.form.getlist("supplier_id")
    qtys = request.form.getlist("qty")

    for pid, sid, qty in zip(product_ids, supplier_ids, qtys):
        if not sid:
            continue
        try:
            qty_val = float(qty)
        except ValueError:
            continue
        if qty_val <= 0:
            continue

        run = SupplierRun.query.filter_by(supplier_id=int(sid), purchase_date=d).first()
        if not run:
            run = SupplierRun(supplier_id=int(sid), purchase_date=d, status="draft")
            db.session.add(run)
            db.session.flush()

        item = SupplierRunItem.query.filter_by(run_id=run.id, product_id=int(pid)).first()
        if item:
            item.qty = qty_val
        else:
            db.session.add(SupplierRunItem(run_id=run.id, product_id=int(pid), qty=qty_val))

    db.session.commit()
    flash("Закуп распределён по поставщикам", "success")
    return redirect(url_for("admin_procurement", date=date_filter))


@app.route("/admin/runs/<int:run_id>")
@admin_required
def admin_run_detail(run_id):
    run = SupplierRun.query.get_or_404(run_id)
    message_text = build_whatsapp_message(run)
    wa_link = f"https://wa.me/{run.supplier.whatsapp_phone}?text={quote(message_text)}"
    return render_template("admin_run_detail.html", run=run, message_text=message_text, wa_link=wa_link)


def build_whatsapp_message(run):
    lines = [f"Заявка на {run.purchase_date.strftime('%d.%m.%Y')}", "restopostavka", ""]
    for item in run.items:
        lines.append(f"- {item.product.name}: {item.qty:g} {item.product.unit}")
    lines.append("")
    lines.append("Спасибо!")
    return "\n".join(lines)


@app.route("/admin/runs/<int:run_id>/send", methods=["POST"])
@admin_required
def admin_run_send(run_id):
    run = SupplierRun.query.get_or_404(run_id)
    run.status = "sent"
    run.sent_at = datetime.utcnow()
    db.session.commit()
    message_text = build_whatsapp_message(run)
    wa_link = f"https://wa.me/{run.supplier.whatsapp_phone}?text={quote(message_text)}"
    return redirect(wa_link)


@app.route("/admin/runs/<int:run_id>/receive", methods=["POST"])
@admin_required
def admin_run_receive(run_id):
    run = SupplierRun.query.get_or_404(run_id)
    item_ids = request.form.getlist("item_id")
    prices = request.form.getlist("actual_price")
    for iid, price in zip(item_ids, prices):
        item = SupplierRunItem.query.get(int(iid))
        if item and price:
            try:
                item.actual_price = float(price)
            except ValueError:
                pass
    run.status = "received"
    run.received_at = datetime.utcnow()
    db.session.commit()
    flash(f"Рейс поставщика «{run.supplier.name}» отмечен как получен", "success")
    return redirect(url_for("admin_run_detail", run_id=run.id))


@app.route("/admin/runs/<int:run_id>/upload_photo", methods=["POST"])
@admin_required
def admin_run_upload_photo(run_id):
    run = SupplierRun.query.get_or_404(run_id)
    file = request.files.get("photo")
    if file and file.filename:
        filename = secure_filename(f"run{run_id}_{datetime.utcnow().timestamp()}_{file.filename}")
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)
        db.session.add(SupplierInvoicePhoto(run_id=run.id, filepath=f"uploads/invoices/{filename}"))
        db.session.commit()
        flash("Фото накладной загружено", "success")
    return redirect(url_for("admin_run_detail", run_id=run.id))


# ---------- Финансы: дебет/кредит ----------

@app.route("/admin/finance/restaurants")
@admin_required
def admin_finance_restaurants():
    restaurants = Restaurant.query.order_by(Restaurant.name).all()
    return render_template("admin_finance_restaurants.html", restaurants=restaurants)


@app.route("/admin/finance/restaurants/<int:restaurant_id>")
@admin_required
def admin_finance_restaurant_detail(restaurant_id):
    restaurant = Restaurant.query.get_or_404(restaurant_id)
    orders = Order.query.filter_by(restaurant_id=restaurant.id).order_by(Order.created_at.desc()).all()
    payments = RestaurantPayment.query.filter_by(restaurant_id=restaurant.id).order_by(RestaurantPayment.created_at.desc()).all()
    return render_template("admin_finance_restaurant_detail.html", restaurant=restaurant, orders=orders, payments=payments)


@app.route("/admin/finance/restaurants/<int:restaurant_id>/add_payment", methods=["POST"])
@admin_required
def admin_finance_restaurant_add_payment(restaurant_id):
    amount = float(request.form.get("amount", 0) or 0)
    note = request.form.get("note", "")
    if amount > 0:
        db.session.add(RestaurantPayment(restaurant_id=restaurant_id, amount=amount, note=note))
        db.session.commit()
        flash(f"Оплата {amount:.0f} ₸ зафиксирована", "success")
    return redirect(url_for("admin_finance_restaurant_detail", restaurant_id=restaurant_id))


@app.route("/admin/finance/suppliers")
@admin_required
def admin_finance_suppliers():
    suppliers = Supplier.query.order_by(Supplier.name).all()
    return render_template("admin_finance_suppliers.html", suppliers=suppliers)


@app.route("/admin/finance/suppliers/<int:supplier_id>")
@admin_required
def admin_finance_supplier_detail(supplier_id):
    supplier = Supplier.query.get_or_404(supplier_id)
    runs = SupplierRun.query.filter_by(supplier_id=supplier.id).order_by(SupplierRun.purchase_date.desc()).all()
    payments = SupplierPayment.query.filter_by(supplier_id=supplier.id).order_by(SupplierPayment.created_at.desc()).all()
    return render_template("admin_finance_supplier_detail.html", supplier=supplier, runs=runs, payments=payments)


@app.route("/admin/finance/suppliers/<int:supplier_id>/add_payment", methods=["POST"])
@admin_required
def admin_finance_supplier_add_payment(supplier_id):
    amount = float(request.form.get("amount", 0) or 0)
    note = request.form.get("note", "")
    if amount > 0:
        db.session.add(SupplierPayment(supplier_id=supplier_id, amount=amount, note=note))
        db.session.commit()
        flash(f"Оплата поставщику {amount:.0f} ₸ зафиксирована", "success")
    return redirect(url_for("admin_finance_supplier_detail", supplier_id=supplier_id))


# ---------- Метрики цен ----------

@app.route("/admin/metrics")
@admin_required
def admin_metrics():
    products = Product.query.order_by(Product.name).all()
    product_id = request.args.get("product_id", type=int)
    selected_product = None
    restaurant_price_history = []
    supplier_price_history = []

    if product_id:
        selected_product = Product.query.get_or_404(product_id)
        items = (
            OrderItem.query.filter_by(product_id=product_id)
            .join(Order)
            .order_by(Order.delivery_date.desc())
            .limit(60)
            .all()
        )
        restaurant_price_history = [
            {"date": item.order.delivery_date, "price": item.price, "restaurant": item.order.restaurant.name, "qty": item.qty}
            for item in items
        ]

        run_items = (
            SupplierRunItem.query.filter_by(product_id=product_id)
            .join(SupplierRun)
            .filter(SupplierRunItem.actual_price.isnot(None))
            .order_by(SupplierRun.purchase_date.desc())
            .limit(60)
            .all()
        )
        supplier_price_history = [
            {"date": ri.run.purchase_date, "price": ri.actual_price, "supplier": ri.run.supplier.name, "qty": ri.qty}
            for ri in run_items
        ]

    return render_template(
        "admin_metrics.html",
        products=products,
        selected_product=selected_product,
        restaurant_price_history=restaurant_price_history,
        supplier_price_history=supplier_price_history,
    )


# ---------- Уведомления ----------

@app.route("/admin/notifications")
@admin_required
def admin_notifications():
    notifications = AdminNotification.query.order_by(AdminNotification.created_at.desc()).limit(100).all()
    payment_notices = PaymentNotice.query.filter_by(status="pending").order_by(PaymentNotice.created_at.desc()).all()

    unread_ids = [n.id for n in notifications if not n.is_read]
    if unread_ids:
        AdminNotification.query.filter(AdminNotification.id.in_(unread_ids)).update(
            {"is_read": True}, synchronize_session=False
        )
        db.session.commit()

    return render_template("admin_notifications.html", notifications=notifications, payment_notices=payment_notices)


@app.route("/admin/payment_notices/<int:notice_id>/accept", methods=["POST"])
@admin_required
def admin_payment_notice_accept(notice_id):
    notice = PaymentNotice.query.get_or_404(notice_id)
    db.session.add(RestaurantPayment(restaurant_id=notice.restaurant_id, amount=notice.amount, note=f"Подтверждено из заявки: {notice.note or ''}"))
    notice.status = "accepted"
    db.session.commit()
    flash(f"Оплата {notice.amount:.0f} ₸ подтверждена и зачислена", "success")
    return redirect(url_for("admin_notifications"))


@app.route("/admin/payment_notices/<int:notice_id>/reject", methods=["POST"])
@admin_required
def admin_payment_notice_reject(notice_id):
    notice = PaymentNotice.query.get_or_404(notice_id)
    notice.status = "rejected"
    db.session.commit()
    flash("Заявка на оплату отклонена", "success")
    return redirect(url_for("admin_notifications"))


# ---------- AI-ассистент ----------

def build_business_snapshot():
    """Собирает компактный срез ключевых данных бизнеса для контекста ассистента."""
    lines = []

    restaurants = Restaurant.query.order_by(Restaurant.name).all()
    if restaurants:
        lines.append("=== Балансы ресторанов (кто нам должен) ===")
        for r in restaurants[:40]:
            lines.append(f"- {r.name}: заказано {r.total_debit:.0f}₸, оплачено {r.total_credit:.0f}₸, баланс {r.balance:.0f}₸")

    suppliers = Supplier.query.order_by(Supplier.name).all()
    if suppliers:
        lines.append("\n=== Балансы поставщиков (кому мы должны) ===")
        for s in suppliers[:40]:
            lines.append(f"- {s.name}: закуп {s.total_debit:.0f}₸, оплачено {s.total_credit:.0f}₸, баланс {s.balance:.0f}₸")

    recent_orders = Order.query.order_by(Order.created_at.desc()).limit(25).all()
    if recent_orders:
        lines.append("\n=== Последние заказы ресторанов ===")
        for o in recent_orders:
            lines.append(f"- №{o.id} {o.restaurant.name}, доставка {o.delivery_date.strftime('%d.%m.%Y')}, статус={o.status_label}, сумма={o.total:.0f}₸")

    disputes = Order.query.filter_by(dispute_flag=True).order_by(Order.updated_at.desc()).limit(15).all()
    if disputes:
        lines.append("\n=== Открытые споры от ресторанов ===")
        for o in disputes:
            lines.append(f"- №{o.id} {o.restaurant.name}: {o.dispute_note or 'без комментария'}")

    notices = PaymentNotice.query.filter_by(status="pending").order_by(PaymentNotice.created_at.desc()).limit(15).all()
    if notices:
        lines.append("\n=== Неподтверждённые заявки на оплату от ресторанов ===")
        for n in notices:
            lines.append(f"- {n.restaurant.name}: {n.amount:.0f}₸ ({n.note or 'без комментария'})")

    runs = SupplierRun.query.order_by(SupplierRun.purchase_date.desc()).limit(20).all()
    if runs:
        lines.append("\n=== Последние закупочные рейсы у поставщиков ===")
        for run in runs:
            total = run.actual_total if run.status == "received" else run.planned_total
            lines.append(f"- {run.supplier.name}, {run.purchase_date.strftime('%d.%m.%Y')}, статус={run.status_label}, сумма≈{total:.0f}₸")

    products_count = Product.query.filter_by(is_active=True).count()
    restaurants_count = len(restaurants)
    lines.append(f"\n=== Прочее ===\nАктивных товаров в каталоге: {products_count}\nВсего ресторанов-клиентов: {restaurants_count}")

    return "\n".join(lines)


@app.route("/admin/assistant/chat", methods=["POST"])
@admin_required
def admin_assistant_chat():
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "Ассистент не настроен: не задан ANTHROPIC_API_KEY на сервере."}), 400

    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()
    history = data.get("history") or []  # [{role: 'user'|'assistant', content: str}, ...]

    if not user_message:
        return jsonify({"error": "Пустое сообщение"}), 400

    snapshot = build_business_snapshot()
    system_prompt = (
        "Ты — AI-ассистент внутри админ-панели сервиса restopostavka (закуп овощей/продуктов "
        "для ресторанов, Алматы, Казахстан). Помогаешь владельцу/закупщику: отвечаешь на вопросы "
        "по текущим данным бизнеса (см. срез ниже), помогаешь составлять сообщения поставщикам "
        "и ресторанам, объясняешь метрики и маржу, даёшь короткие практичные советы. "
        "Отвечай по-русски, кратко и по делу, используй суммы в тенге (₸). "
        "Если в срезе данных нет ответа — так и скажи, не выдумывай цифры.\n\n"
        f"Текущий срез данных бизнеса:\n{snapshot}"
    )

    messages = []
    for turn in history[-10:]:
        role = turn.get("role")
        content = turn.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 1024,
                "system": system_prompt,
                "messages": messages,
            },
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        reply_text = "".join(
            block.get("text", "") for block in result.get("content", []) if block.get("type") == "text"
        ).strip()
        if not reply_text:
            reply_text = "Не удалось получить ответ от ассистента."
        return jsonify({"reply": reply_text})
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Ошибка обращения к AI: {e}"}), 500


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
