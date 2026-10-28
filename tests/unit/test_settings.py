from splitledger.settings import Settings


# urls from a host are switched to the async driver, ours are left alone
def test_database_url_driver():
    hosted = Settings(database_url="postgresql://u:p@db.internal:5432/railway")
    assert hosted.database_url == "postgresql+asyncpg://u:p@db.internal:5432/railway"
    short = Settings(database_url="postgres://u:p@h/db")
    assert short.database_url == "postgresql+asyncpg://u:p@h/db"
    ours = "postgresql+asyncpg://splitledger:splitledger@localhost:5432/splitledger"
    assert Settings(database_url=ours).database_url == ours
