from app import create_app, db
from sqlalchemy import text

app = create_app()

with app.app_context():
    # 1. Força a exclusão de TODAS as tabelas e relacionamentos na marra (CASCADE)
    db.session.execute(text('''
        DO $$ DECLARE
            r RECORD;
        BEGIN
            FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP
                EXECUTE 'DROP TABLE IF EXISTS public.' || quote_ident(r.tablename) || ' CASCADE';
            END LOOP;
        END $$;
    '''))
    db.session.commit()
    
    # 2. Cria as tabelas do zero com as colunas corretas (incluindo o username)
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)