from terricon_events_bot.domain.enums import CategorySlug, Locale, SourceTheme


def test_category_contract_is_closed_and_complete() -> None:
    assert {category.value for category in CategorySlug} == {
        "development_it",
        "ai_data",
        "business_finance",
        "marketing_sales",
        "design_creative",
        "startups_products",
        "career_education_hr",
        "soft_skills_languages",
        "other",
    }


def test_source_contract_has_exactly_six_locale_theme_combinations() -> None:
    combinations = {(locale, theme) for locale in Locale for theme in SourceTheme}

    assert len(combinations) == 6
