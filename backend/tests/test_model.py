from __future__ import annotations
import circuit_seq_server.model as model
import datetime
import pathlib
from freezegun import freeze_time
from werkzeug.datastructures import FileStorage


def _count_settings() -> int:
    return len(
        model.db.session.execute(model.db.select(model.Settings)).scalars().all()
    )


def test_settings(app, tmp_path):
    with app.app_context():
        assert _count_settings() == 0
        settings = model.get_current_settings()
        assert _count_settings() == 1
        assert settings["plate_n_rows"] == 8
        assert settings["plate_n_cols"] == 12
        settings["plate_n_rows"] = 14
        settings["plate_n_cols"] = 18
        email = "test@test.com"
        msg, code = model.set_current_settings(email, settings)
        assert code == 200
        assert "Settings updated" in msg
        assert _count_settings() == 2
        new_settings = model.get_current_settings()
        assert new_settings["plate_n_rows"] == 14
        assert new_settings["plate_n_cols"] == 18
        # settings dict missing required fields is a no-op
        msg, code = model.set_current_settings(email, {"plate_n_rows": 10})
        assert code == 401
        assert "settings not updated" in msg
        assert _count_settings() == 2
        assert new_settings["plate_n_rows"] == 14
        assert new_settings["plate_n_cols"] == 18
        msg, code = model.set_current_settings(
            email,
            {
                "plate_n_rows": 10,
                "plate_n_cols": 2,
                "running_options": ["x", "y"],
                "last_submission_day": 5,
            },
        )
        assert code == 200
        assert "Settings updated" in msg
        assert _count_settings() == 3
        new_settings = model.get_current_settings()
        assert new_settings["plate_n_rows"] == 10
        assert new_settings["plate_n_cols"] == 2
        assert new_settings["running_options"] == ["x", "y"]
        assert new_settings["last_submission_day"] == 5


@freeze_time("2022-11-21")
def test_add_new_sample_mon(app, tmp_path):
    with app.app_context():
        current_date = datetime.date.today()
        this_week_samples = model.db.select(model.Sample).filter(
            model.Sample.date >= current_date
        )
        assert model._count_samples_this_week(current_date) == 0
        assert model.remaining_samples_this_week(current_date)["remaining"] == 96
        # add a sample without a reference sequence
        new_sample, error_message = model.add_new_sample(
            "u1@embl.de", "s1", "running option", 234, None, str(tmp_path)
        )
        assert error_message == ""
        assert new_sample is not None
        assert new_sample.email == "u1@embl.de"
        assert new_sample.name == "s1"
        assert new_sample.running_option == "running option"
        assert new_sample.concentration == 234
        year, week, day = current_date.isocalendar()
        assert new_sample.primary_key == f"{year%100}_{week}_A1"
        assert new_sample.date == current_date
        assert new_sample.reference_sequence_description is None
        assert new_sample.has_results_zip is False
        assert new_sample.has_results_fasta is False
        assert new_sample.has_results_gbk is False
        samples = model.db.session.execute(this_week_samples).scalars().all()
        assert len(samples) == 1
        assert samples[0] == new_sample
        assert model._count_samples_this_week(current_date) == 1
        assert model.remaining_samples_this_week(current_date)["remaining"] == 95


@freeze_time("2022-11-26")
def test_add_new_sample_sat(app, tmp_path):
    with app.app_context():
        current_date = datetime.date.today()
        assert model._count_samples_this_week(current_date) == 0
        assert model.remaining_samples_this_week(current_date)["remaining"] == 0
        assert (
            model.remaining_samples_this_week(current_date)["message"]
            == "Sample submission is closed for this week."
        )
        # try to add a sample on a saturday
        new_sample, error_message = model.add_new_sample(
            "u1@embl.de", "s1", "running option", 123, None, str(tmp_path)
        )
        assert new_sample is None
        assert "closed" in error_message
        assert model._count_samples_this_week(current_date) == 0
        assert model.remaining_samples_this_week(current_date)["remaining"] == 0
        assert (
            model.remaining_samples_this_week(current_date)["message"]
            == "Sample submission is closed for this week."
        )
        settings = model.get_current_settings()
        # make last submission day saturday
        settings["last_submission_day"] = 6
        model.set_current_settings("a@embl.de", settings)
        assert model.remaining_samples_this_week(current_date)["remaining"] == 96
        assert model.remaining_samples_this_week(current_date)["message"] == ""
        # try to add a sample on a saturday
        new_sample, error_message = model.add_new_sample(
            "u1@embl.de", "s1", "running option", 123, None, str(tmp_path)
        )
        assert new_sample is not None
        assert error_message == ""
        assert model._count_samples_this_week(current_date) == 1
        assert model.remaining_samples_this_week(current_date)["remaining"] == 95
        assert model.remaining_samples_this_week(current_date)["message"] == ""


@freeze_time("2022-11-21")
def test_add_new_sample_full(app, tmp_path):
    with app.app_context():
        current_date = datetime.date.today()
        settings = model.get_current_settings()
        settings["plate_n_rows"] = 1
        settings["plate_n_cols"] = 1
        model.set_current_settings("a@embl.de", settings)
        assert model._count_samples_this_week(current_date) == 0
        assert model.remaining_samples_this_week(current_date)["remaining"] == 1
        new_sample, error_message = model.add_new_sample(
            "u1@embl.de", "s1", "running option", 11, None, str(tmp_path)
        )
        assert new_sample is not None
        assert error_message == ""
        assert model._count_samples_this_week(current_date) == 1
        assert model.remaining_samples_this_week(current_date)["remaining"] == 0
        assert (
            model.remaining_samples_this_week(current_date)["message"]
            == "All samples have been taken this week."
        )
        new_sample, error_message = model.add_new_sample(
            "u1@embl.de", "s2", "running option", 66, None, str(tmp_path)
        )
        assert new_sample is None
        assert "samples have been taken this week" in error_message


def _count_users() -> int:
    return len(model.db.session.execute(model.db.select(model.User)).scalars().all())


def test_add_new_user_invalid(app):
    password_valid = "abcABC123"
    email_valid = "joe.bloggs@embl.de"
    with app.app_context():
        for email in ["joe@gmail.com", "@embl.de"]:
            msg, code = model.add_new_user(email, password_valid, is_admin=False)
            assert code == 401
            assert "email" in msg
        for password in [
            "",
            "abc123A",
            "passwordpassword",
            "abc12345678",
            "asd!(*&@#@!(*#%ASDASDFGK",
        ]:
            msg, code = model.add_new_user(email_valid, password, is_admin=False)
            assert code == 401
            assert "Password" in msg
        msg, code = model.add_new_user("user@embl.de", password_valid, is_admin=False)
        assert code == 401
        assert msg == "This email address is already in use"


def test_add_new_user_valid(app):
    email = "x@embl.de"
    password = "passwdP1"
    with app.app_context():
        n_users = _count_users()
        msg, code = model.add_new_user(email, password, is_admin=False)
        assert code == 200
        assert _count_users() == n_users + 1
        user = model.db.session.execute(
            model.db.select(model.User).filter(model.User.email == email)
        ).scalar_one_or_none()
        assert user is not None
        assert user.email == email
        assert user.is_admin is False
        assert user.activated is False
        email_msg = app.config["TESTING_ONLY_LAST_SMTP_MESSAGE"]
        assert email_msg["To"] == email
        # extract activation token from email contents
        activation_token = email_msg.get_content().split("/")[-1].strip()
        # check password pre-activation
        assert user.check_password("wrong") is False
        assert user.check_password(password) is True
        # activate account with invalid token
        model.activate_user("not_a_real_activation_token")
        assert user.activated is False
        model.activate_user(activation_token)
        assert user.activated is True
        # set new password
        assert user.set_password("wrong", "new") is False
        assert user.check_password(password) is True
        assert user.set_password(password, "newPassword2") is True
        assert user.activated is True
        # check new password
        assert user.check_password(password) is False
        assert user.check_password("newPassword2") is True
        assert user.activated is True


def test_process_result_valid(app, result_zipfiles, tmp_path):
    with app.app_context():
        last_email_msg = app.config.get("TESTING_ONLY_LAST_SMTP_MESSAGE")
        assert last_email_msg is None
        for result_zipfile in result_zipfiles:
            with open(result_zipfile, "rb") as f:
                message, code = model.process_result(FileStorage(f), str(tmp_path))
            assert code == 200
            zip_file_path = pathlib.Path(result_zipfile.name)
            assert result_zipfile.name in message
            results_dir = tmp_path / "2022/46/results"
            assert results_dir.is_dir()
            zip_path_on_server = results_dir / zip_file_path
            assert zip_path_on_server.is_file()
            assert zip_path_on_server.with_suffix(".fasta").is_file()
            assert zip_path_on_server.with_suffix(".gbk").is_file()
            last_email_msg = app.config.get("TESTING_ONLY_LAST_SMTP_MESSAGE")
            assert last_email_msg is not None
            assert zip_file_path.stem in str(last_email_msg.get_body())
            email_attachments = [
                attachment.get_filename()
                for attachment in last_email_msg.iter_attachments()
            ]
            assert str(zip_file_path.with_suffix(".fasta")) in email_attachments
            assert str(zip_file_path.with_suffix(".gbk")) in email_attachments
