import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from scripts import install


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='codex-release-test-')
        self.root=Path(self.temp.name)
        self.profile=self.root/'profile';self.profile.mkdir()
        self.codex=self.root/'codex';self.codex.mkdir()
        self.profile_file=self.profile/'cordis.patch.yml'
        self.original='- id: permission\n  config:\n    defaultPreset: workspace-write\n- id: example\n  config: !!js function() { return "keep" }\n'
        self.profile_file.write_text(self.original,encoding='utf-8')
        self.config=self.codex/'config.toml'
        self.config.write_text('[features]\nexample = true\n',encoding='utf-8')
        self.addCleanup(self.temp.cleanup)

    def apply(self):
        return install.apply(install.ROOT,self.profile,self.codex,self.root/'state','C:/Python/python.exe',secure_state=lambda p:None)

    def test_install_preserves_user_settings_and_writes_portable_config_and_skill(self):
        paths=self.apply()
        self.assertEqual(len(paths),5)
        profile=self.profile_file.read_text(encoding='utf-8')
        self.assertTrue(profile.startswith(self.original))
        self.assertEqual(profile.count('id: codex-deepseek-connector'),1)
        config=tomllib.loads(self.config.read_text(encoding='utf-8'))
        self.assertTrue(config['features']['example'])
        self.assertEqual(config['mcp_servers']['deepseek']['command'],'C:/Python/python.exe')
        self.assertEqual(len(list(self.profile.glob('cordis.patch.yml.backup-*'))),1)
        skill=(self.codex/'skills/deepseek-delegate/SKILL.md').read_text(encoding='utf-8')
        self.assertNotIn('@@BRIDGE_ROOT@@',skill)
        self.assertIn('explicit',skill)

    def test_existing_server_or_skill_is_never_overwritten(self):
        self.config.write_text('[mcp_servers.deepseek]\ncommand = "keep"\n',encoding='utf-8')
        before=self.config.read_bytes()
        with self.assertRaisesRegex(ValueError,'already exists'):self.apply()
        self.assertEqual(self.config.read_bytes(),before)
        self.assertEqual(self.profile_file.read_text(encoding='utf-8'),self.original)
        self.config.write_text('',encoding='utf-8')
        (self.codex/'skills/deepseek-delegate').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError,'skill already exists'):self.apply()
        self.assertEqual(self.profile_file.read_text(encoding='utf-8'),self.original)

    def test_unmanaged_connector_is_rejected_and_managed_block_replaces_once(self):
        with self.assertRaisesRegex(ValueError,'unmanaged'):install.replace_block('- id: codex-deepseek-connector\n','patch')
        patch_text=install.artifacts(install.ROOT,self.profile,self.root/'state','python')[0]
        first=install.replace_block(self.original,patch_text)
        second=install.replace_block(first,patch_text)
        self.assertEqual(second.count('id: codex-deepseek-connector'),1)
        self.assertTrue(second.startswith(self.original))

    def test_written_files_roll_back_if_later_write_fails(self):
        original_write=Path.write_text
        def fail_skill(path,*args,**kwargs):
            if path.name=='SKILL.md':raise OSError('Simulated write failure')
            return original_write(path,*args,**kwargs)
        with patch.object(Path,'write_text',fail_skill):
            with self.assertRaisesRegex(OSError,'Simulated'):self.apply()
        self.assertEqual(self.profile_file.read_text(encoding='utf-8'),self.original)
        self.assertEqual(self.config.read_text(encoding='utf-8'),'[features]\nexample = true\n')
        self.assertFalse((self.profile/'codex-deepseek-connector.mjs').exists())


if __name__=='__main__':unittest.main()
