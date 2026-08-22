import os
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import quote

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.utils import secure_filename
from models import (
    db, User, Restaurant, Product, Category, RestaurantPrice, Order, OrderItem, ORDER_STATUSES,
    Supplier, SupplierRun, SupplierRunItem, SupplierInvoicePhoto, RUN_STATUSES,
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'restopostavka.db')}"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = os.path.join(BASE_DIR, "static", "uploads", "invoices")
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

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


@app.context_processor
def inject_globals():
    return {"current_user": current_user(), "now": datetime.now()}


# ---------- Авторизация ----------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(phone=phone).first()
        if user and user.check_password(password):
            session["user_id"] = user.id
            flash(f"Добро пожаловать, {user.name}!", "success")
            if user.is_admin:
                return redirect(url_for("admin_orders"))
            return redirect(url_for("catalog"))
        flash("Неверный телефон или пароль", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
    flash(f"По заказу №{order.id} отправлено уведомление о расхождении", "success")
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
    restaurants = Restaurant.query.order_by(Restaurant.name).all()
    return render_template("admin_restaurants.html", restaurants=restaurants)


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
    lines = [f"Заявка на {run.purchase_date.strftime('%d.%m.%Y')}", "КрафтСнаб", ""]
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


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
