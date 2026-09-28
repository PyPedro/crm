from app import create_app, db

app = create_app()

with app.app_context():
    db.drop_all()  # Limpa o histórico de tabelas defeituosas
    db.create_all() # Cria as tabelas novinhas em folha

if __name__ == '__main__':
    app.run(debug=True)