"""Создаёт таблицы и наполняет базу стартовыми данными: категории, товары, админ, тестовый ресторан."""
from app import app
from models import db, Category, Product, User, Restaurant, Supplier

CATEGORIES = ["Овощи", "Зелень", "Фрукты", "Молочные продукты", "Бакалея", "Мясо", "Хозтовары", "Фирма"]

PRODUCTS = [
    ("Картофель", "Овощи", "кг", 180),
    ("Морковь", "Овощи", "кг", 150),
    ("Лук репчатый", "Овощи", "кг", 130),
    ("Помидоры", "Овощи", "кг", 650),
    ("Огурцы", "Овощи", "кг", 500),
    ("Капуста белокочанная", "Овощи", "кг", 120),
    ("Перец болгарский", "Овощи", "кг", 700),
    ("Укроп", "Зелень", "кг", 900),
    ("Петрушка", "Зелень", "кг", 900),
    ("Кинза", "Зелень", "кг", 900),
    ("Лимоны", "Фрукты", "кг", 800),
    ("Яблоки", "Фрукты", "кг", 400),
    ("Молоко 3.2%", "Молочные продукты", "л", 450),
    ("Сметана 20%", "Молочные продукты", "кг", 900),
    ("Масло растительное", "Бакалея", "л", 700),
    ("Рис", "Бакалея", "кг", 550),
    ("Куриное филе", "Мясо", "кг", 2200),
    ("Говядина", "Мясо", "кг", 3200),
    ("Фарш говяжий", "Мясо", "кг", 2800),
    ("Перчатки одноразовые", "Хозтовары", "уп", 1200),
    ("Пакеты для мусора", "Хозтовары", "уп", 900),
    ("Средство для мытья посуды", "Хозтовары", "л", 1100),
    ("Кетчуп фирменный", "Фирма", "кг", 1300),
    ("Соус фирменный", "Фирма", "кг", 1500),
]


def run():
    with app.app_context():
        db.create_all()

        cat_map = {}
        for i, name in enumerate(CATEGORIES):
            cat = Category.query.filter_by(name=name).first()
            if not cat:
                cat = Category(name=name, sort_order=i)
                db.session.add(cat)
                db.session.flush()
            cat_map[name] = cat

        for name, cat_name, unit, price in PRODUCTS:
            if not Product.query.filter_by(name=name).first():
                db.session.add(Product(name=name, unit=unit, base_price=price, category_id=cat_map[cat_name].id))

        if not User.query.filter_by(phone="admin").first():
            admin = User(name="Админ", phone="admin", role="admin")
            admin.set_password("admin123")
            db.session.add(admin)

        demo_restaurant = Restaurant.query.filter_by(name="Тестовый ресторан").first()
        if not demo_restaurant:
            demo_restaurant = Restaurant(name="Тестовый ресторан", address="Алматы", phone="+77770000000")
            db.session.add(demo_restaurant)
            db.session.flush()

        if not User.query.filter_by(phone="77770000000").first():
            demo_user = User(name="Менеджер ресторана", phone="77770000000", role="restaurant", restaurant_id=demo_restaurant.id)
            demo_user.set_password("demo123")
            db.session.add(demo_user)

        if not Supplier.query.filter_by(name="Барахолка — овощи").first():
            db.session.add(Supplier(name="Барахолка — овощи", whatsapp_phone="77011112233", notes="Картофель, морковь, лук"))
        if not Supplier.query.filter_by(name="Зелёный рынок — зелень").first():
            db.session.add(Supplier(name="Зелёный рынок — зелень", whatsapp_phone="77022223344", notes="Зелень, помидоры, огурцы"))

        db.session.commit()
        print("Готово. Логины:")
        print("  Админ:    phone=admin       password=admin123")
        print("  Ресторан: phone=77770000000 password=demo123")


if __name__ == "__main__":
    run()
