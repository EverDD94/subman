package org.jellyfin.androidtv.util

import androidx.activity.viewModels
import androidx.fragment.app.FragmentActivity
import androidx.lifecycle.ViewModel
import org.jellyfin.androidtv.R
import org.jellyfin.androidtv.preference.UserPreferences
import org.jellyfin.androidtv.preference.constant.AppTheme
import org.koin.android.ext.android.inject
import timber.log.Timber

/**
 * Getter to get the style resource for a given theme. Everflix has a single look, so an older
 * theme choice left in the preferences still gets it.
 */
@Suppress("UnusedReceiverParameter")
private val AppTheme.style
	get() = R.style.Theme_Jellyfin

/**
 * Private view model for the [applyTheme] extension to store the currently set theme.
 */
class ThemeViewModel : ViewModel() {
	var theme: AppTheme? = null
}

/**
 * Extension function to set the theme. Should be called in [FragmentActivity.onCreate] and
 * [FragmentActivity.onResume]. It recreates the activity when the theme changed after it was set.
 * Do not call during resume if the activity may not be recreated (like in the video player).
 */
fun FragmentActivity.applyTheme() {
	val viewModel by viewModels<ThemeViewModel>()
	val userPreferences by inject<UserPreferences>()
	val theme = userPreferences[UserPreferences.appTheme]

	if (viewModel.theme != theme) {
		if (viewModel.theme != null) {
			Timber.i("Recreating activity to apply theme")
			viewModel.theme = null
			recreate()
		} else {
			Timber.i("Applying theme $theme")
			viewModel.theme = theme
			setTheme(theme.style)
		}
	}
}
