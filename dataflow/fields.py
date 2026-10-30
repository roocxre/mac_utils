"""The fallback field classifier, for fields the keyword rules in extension/fields.js can't place.

A field's context (its label, placeholder, aria-label, name, id, nearby text) is embedded with
MiniLM (models.embed) and a small logistic-regression head picks the data type. The head is
trained once, in a second, from the hand-written examples below (paraphrases and languages the
rules don't cover): `.venv/bin/python -m dataflow train-fields`. Below the threshold the field
stays "other".
"""
import os

import numpy as np

from dataflow import MODELS_DIR, lists

TYPES = ("email", "name", "phone", "address", "birthday", "payment", "password", "other")
EXAMPLES = {
    "email": ["Where should we send your receipt?", "Your inbox", "E-posta adresi", "Adres e-mail", "E-postadress",
              "Sähköposti", "电邮地址", "Endereço de e-mail", "Indirizzo e-mail", "Электронный адрес", "Your work address for updates",
              "Get 10% off: enter your best address", "Subscribe: address", "Mailing list signup", "you@example.com",
              "name@company.com", "Join the newsletter", "Send me the code at", "Login ID (your address)", "Inbox for deals",
              "E-Mail-Adresse", "Adresse électronique", "Dirección de correo", "メールアドレス", "Your newsletter address"],
    "name": ["What should we call you?", "Achternaam", "Voornaam", "Nazwisko", "Imię", "Efternamn", "Förnamn",
             "Sobrenome", "Soyadı", "Adınız", "Etunimi", "Sukunimi", "Familienname", "Full legal name", "Your name",
             "Recipient", "Who is this for?", "Name as it appears on ID", "Jane Doe", "Middle initial",
             "Nom de famille", "Primer apellido", "Cognome e nome", "お名前", "Ваше имя"],
    "phone": ["Your digits", "How can we text you?", "Telefoonnummer", "Numer telefonu", "Telefonnummer",
              "Puhelinnumero", "Número de celular", "Cep telefonu", "Mobilnummer", "+1 (555) 000-0000", "SMS updates number",
              "Daytime contact number", "Text me the code", "WhatsApp number", "Rufnummer", "Numéro de portable",
              "Teléfono móvil", "Numero di cellulare", "携帯電話番号", "Номер мобильного", "Where can we reach you by text?",
              "Contact number", "Landline", "Your cell", "Mobile"],
    "address": ["Ship to", "Where should we deliver?", "Straat en huisnummer", "Postcode", "Kod pocztowy", "Ulica",
                "Gatuadress", "Postnummer", "Ort", "Wohnort", "Apartment, suite, etc.", "Apt / Unit", "House number",
                "Delivery instructions location", "Town", "Region", "Bairro", "Rua", "Município", "Billing address line 2",
                "Código postal", "CAP", "郵便番号", "Почтовый индекс", "Where do you live?"],
    "birthday": ["When were you born?", "Geboortedatum", "Data urodzenia", "Födelsedatum", "Syntymäaika",
                 "Data de nascimento", "Doğum tarihi", "Year born", "Birth month", "Your age", "MM/DD/YYYY (birthday)",
                 "Celebrate with us: your special day", "Fecha de nacimiento", "Data di nascita", "生年月日",
                 "Дата рождения", "Age verification: date", "Born on", "Your birthday", "Day you were born"],
    "payment": ["Card digits", "Kaartnummer", "Numer karty", "Kortnummer", "Número do cartão", "Kart numarası",
                "MM / YY", "Expiry", "3-digit code on back", "Security digits", "Bank account", "Sort code",
                "Routing number", "IBAN", "Card verification", "Name on card", "Numéro de carte", "Número de tarjeta",
                "Kartennummer", "カード番号"],
    "password": ["Choose a passphrase", "Wachtwoord", "Hasło", "Lösenord", "Salasana", "Senha", "Şifre",
                 "Secret phrase", "Confirm your secret", "PIN", "Create a strong one (8+ characters)", "Kennwort",
                 "Mot de passe", "Contraseña", "パスワード", "Пароль", "Repeat it"],
    "other": ["Search", "Search products", "Quantity", "Qty", "Promo code", "Coupon", "Gift card code", "Comments",
              "Your message", "Subject", "Company", "Job title", "Website", "Order number", "How did you hear about us?",
              "Notes", "Size", "Color", "Rate this product", "Review title", "Write a review", "Keyword", "Zoek",
              "Szukaj", "Sök", "Pesquisar", "Ara", "Captcha", "Enter the characters you see", "Verification code",
              "One-time code", "Referral code", "Gift message", "Tracking number", "SKU", "Price", "Amount",
              "Number of guests", "Check-in date", "Departure city"],
}

_head = {}


def head_path():
    return os.path.join(MODELS_DIR, lists.config()["fields"]["head"])


def context_text(context):
    """One line of words for a field: what you can read first, then the markup's names."""
    parts = [context.get(k, "") for k in ("label", "aria", "placeholder", "title", "nearby")]
    names = " ".join(context.get(k, "") for k in ("name", "id", "autocomplete"))
    names = " ".join(names.replace("_", " ").replace("-", " ").split())
    return " | ".join(p for p in parts + [names] if p and p.strip())[:300]


def train():
    from dataflow import models
    from sklearn.linear_model import LogisticRegression

    texts, labels = [], []
    for label, examples in EXAMPLES.items():
        texts += examples
        labels += [label] * len(examples)
    vectors = models.embed(texts)
    model = LogisticRegression(C=8.0, max_iter=2000)
    model.fit(vectors, labels)
    accuracy = float(model.score(vectors, labels))
    np.savez(head_path(), coef=model.coef_.astype(np.float32), intercept=model.intercept_.astype(np.float32),
             classes=np.array(model.classes_))
    _head.clear()
    return {"examples": len(texts), "train_accuracy": round(accuracy, 3), "saved": head_path()}


def _load():
    if not _head:
        data = np.load(head_path())
        _head.update(coef=data["coef"], intercept=data["intercept"], classes=[str(c) for c in data["classes"]])
    return _head


def classify(contexts):
    """{"types": [{"type", "p"}]} for each field context; "other" when unsure."""
    from dataflow import models

    threshold = lists.config()["fields"]["threshold"]
    texts = [context_text(c or {}) for c in contexts]
    if not os.path.exists(head_path()) or not any(texts):
        return {"types": [{"type": "other", "p": 0} for _ in texts]}
    head = _load()
    out = []
    vectors = models.embed([t or "field" for t in texts])
    logits = vectors @ head["coef"].T + head["intercept"]
    for text, row in zip(texts, logits):
        probs = np.exp(row - row.max())
        probs /= probs.sum()
        best = int(probs.argmax())
        label = head["classes"][best]
        out.append({"type": label if text and probs[best] >= threshold else "other", "p": round(float(probs[best]), 2)})
    return {"types": out}
