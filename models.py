from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class Restaurant(db.Model):
    __tablename__ = "restaurants"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    address = db.Column(db.String(300))
    phone = db.Column(db.String(50))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    users = db.relationship("User", backref="restaurant", lazy=True)
    orders = db.relationship("Order", backref="restaurant", lazy=True)
    price_overrides = db.relationship("RestaurantPrice", backref="restaurant", lazy=True)
    payments = db.relationship("RestaurantPayment", backref="restaurant", lazy=True)

    def price_for(self, product):
        override = RestaurantPrice.query.filter_by(
            restaurant_id=self.id, product_id=product.id
        ).first()
        return override.price if override else product.base_price

    @property
    def total_debit(self):
        return sum(o.total for o in self.orders if o.status != "cancelled")

    @property
    def total_credit(self):
        return sum(p.amount for p in self.payments)

    @property
    def balance(self):
        """Положительный баланс = ресторан должен нам."""
        return self.total_debit - self.total_credit


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="restaurant")  # restaurant | admin | warehouse
    restaurant_id = db.Column(db.Integer, db.ForeignKey("restaurants.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Face ID / Touch ID (WebAuthn passkey)
    webauthn_credential_id = db.Column(db.String(500), nullable=True)
    webauthn_public_key = db.Column(db.String(500), nullable=True)
    webauthn_sign_count = db.Column(db.Integer, default=0)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role in ("admin", "warehouse")

    @property
    def has_faceid(self):
        return bool(self.webauthn_credential_id)


class Category(db.Model):
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    sort_order = db.Column(db.Integer, default=0)

    products = db.relationship("Product", backref="category", lazy=True)


class Product(db.Model):
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    unit = db.Column(db.String(20), nullable=False, default="кг")  # кг, шт, ящик, л
    base_price = db.Column(db.Float, nullable=False, default=0)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"))
    is_active = db.Column(db.Boolean, default=True)
    image_url = db.Column(db.String(300))

    def __repr__(self):
        return f"<Product {self.name}>"


class RestaurantPrice(db.Model):
    __tablename__ = "restaurant_prices"

    id = db.Column(db.Integer, primary_key=True)
    restaurant_id = db.Column(db.Integer, db.ForeignKey("restaurants.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    price = db.Column(db.Float, nullable=False)

    product = db.relationship("Product")

    __table_args__ = (db.UniqueConstraint("restaurant_id", "product_id"),)


ORDER_STATUSES = ["new", "confirmed", "packing", "on_the_way", "delivered", "cancelled"]

STATUS_LABELS = {
    "new": "Новый",
    "confirmed": "Подтверждён",
    "packing": "Собирается",
    "on_the_way": "В пути",
    "delivered": "Доставлен",
    "cancelled": "Отменён",
}


class Order(db.Model):
    __tablename__ = "orders"

    id = db.Column(db.Integer, primary_key=True)
    restaurant_id = db.Column(db.Integer, db.ForeignKey("restaurants.id"), nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    delivery_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), default="new")
    comment = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Передача водителем ресторану
    delivered_at = db.Column(db.DateTime, nullable=True)
    delivered_note = db.Column(db.String(300))

    # Подтверждение/спор со стороны ресторана
    restaurant_confirmed_at = db.Column(db.DateTime, nullable=True)
    dispute_flag = db.Column(db.Boolean, default=False)
    dispute_note = db.Column(db.Text)

    items = db.relationship("OrderItem", backref="order", lazy=True, cascade="all, delete-orphan")
    dispute_photos = db.relationship("OrderDisputePhoto", backref="order", lazy=True, cascade="all, delete-orphan")

    @property
    def total(self):
        return sum(item.qty * item.price for item in self.items)

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def confirmation_state(self):
        if self.dispute_flag:
            return "dispute"
        if self.restaurant_confirmed_at:
            return "confirmed"
        if self.delivered_at:
            return "awaiting_confirmation"
        return "not_delivered"


class OrderItem(db.Model):
    __tablename__ = "order_items"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    qty = db.Column(db.Float, nullable=False)
    price = db.Column(db.Float, nullable=False)  # цена на момент заказа
    packed_qty = db.Column(db.Float, nullable=True)  # фактический вес при фасовке (со сканера весов)

    product = db.relationship("Product")

    @property
    def subtotal(self):
        return self.qty * self.price

    @property
    def weight_diff(self):
        """Разница между заказанным и фактически расфасованным весом."""
        if self.packed_qty is None:
            return None
        return round(self.packed_qty - self.qty, 2)


# ---------- Закуп у поставщиков ----------

class Supplier(db.Model):
    __tablename__ = "suppliers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    whatsapp_phone = db.Column(db.String(50), nullable=False)  # формат: 77001234567 (без +, без пробелов)
    notes = db.Column(db.String(300))
    is_active = db.Column(db.Boolean, default=True)

    runs = db.relationship("SupplierRun", backref="supplier", lazy=True)
    payments = db.relationship("SupplierPayment", backref="supplier", lazy=True)

    @property
    def total_debit(self):
        return sum(r.actual_total for r in self.runs if r.status == "received")

    @property
    def total_credit(self):
        return sum(p.amount for p in self.payments)

    @property
    def balance(self):
        """Положительный баланс = мы должны поставщику."""
        return self.total_debit - self.total_credit


RUN_STATUSES = ["draft", "sent", "received"]
RUN_STATUS_LABELS = {"draft": "Черновик", "sent": "Отправлена", "received": "Получена"}


class SupplierRun(db.Model):
    """Одна заявка одному поставщику на конкретную дату закупа."""
    __tablename__ = "supplier_runs"

    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=False)
    purchase_date = db.Column(db.Date, nullable=False)  # дата, на которую собираем закуп (= delivery_date заказов)
    status = db.Column(db.String(20), default="draft")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    sent_at = db.Column(db.DateTime, nullable=True)
    received_at = db.Column(db.DateTime, nullable=True)

    items = db.relationship("SupplierRunItem", backref="run", lazy=True, cascade="all, delete-orphan")
    photos = db.relationship("SupplierInvoicePhoto", backref="run", lazy=True, cascade="all, delete-orphan")

    __table_args__ = (db.UniqueConstraint("supplier_id", "purchase_date"),)

    @property
    def status_label(self):
        return RUN_STATUS_LABELS.get(self.status, self.status)

    @property
    def planned_total(self):
        return sum(i.qty * (i.actual_price or i.product.base_price) for i in self.items)

    @property
    def actual_total(self):
        return sum(i.qty * i.actual_price for i in self.items if i.actual_price)


class SupplierRunItem(db.Model):
    __tablename__ = "supplier_run_items"

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey("supplier_runs.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    qty = db.Column(db.Float, nullable=False)
    actual_price = db.Column(db.Float, nullable=True)  # заполняется после получения накладной

    product = db.relationship("Product")

    __table_args__ = (db.UniqueConstraint("run_id", "product_id"),)


class SupplierInvoicePhoto(db.Model):
    __tablename__ = "supplier_invoice_photos"

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey("supplier_runs.id"), nullable=False)
    filepath = db.Column(db.String(300), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


class OrderDisputePhoto(db.Model):
    __tablename__ = "order_dispute_photos"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False)
    filepath = db.Column(db.String(300), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


# ---------- Финансы: дебет/кредит ----------

class RestaurantPayment(db.Model):
    """Оплата, поступившая от ресторана (кредит, уменьшает долг ресторана)."""
    __tablename__ = "restaurant_payments"

    id = db.Column(db.Integer, primary_key=True)
    restaurant_id = db.Column(db.Integer, db.ForeignKey("restaurants.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SupplierPayment(db.Model):
    """Оплата, отправленная поставщику (кредит, уменьшает наш долг поставщику)."""
    __tablename__ = "supplier_payments"

    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ---------- Уведомления для админа ----------

class AdminNotification(db.Model):
    """Внутриигровое уведомление: спор, подтверждение приёмки, заявка на оплату и т.д."""
    __tablename__ = "admin_notifications"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(30), nullable=False)  # confirmation | dispute | payment_notice
    message = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


PAYMENT_NOTICE_STATUSES = ["pending", "accepted", "rejected"]


class PaymentNotice(db.Model):
    """Ресторан сообщает, что оплатил (полностью/частично) — админ подтверждает вручную."""
    __tablename__ = "payment_notices"

    id = db.Column(db.Integer, primary_key=True)
    restaurant_id = db.Column(db.Integer, db.ForeignKey("restaurants.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(300))
    status = db.Column(db.String(20), default="pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    restaurant = db.relationship("Restaurant")



